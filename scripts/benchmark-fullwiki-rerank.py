#!/usr/bin/env python3
"""Offline semantic-order experiment around the genuine full-wiki LAB pipeline.

No candidate injection, runtime provider, downloads, or changed reading policy.
Reports contain private source text; keep real-corpus outputs outside the repo.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import socket
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lich_agent_bridge import research
from lich_agent_bridge.knowledge import KnowledgeBase, _terms, _CURATED_QUERY_NOISE
from lich_agent_bridge.settings import OnlineFallbackPolicy

spec = importlib.util.spec_from_file_location('optional_semantic', Path(__file__).with_name('benchmark-semantic-retrieval.py'))
semantic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(semantic)
MODES = ('production', 'semantic_sources', 'semantic_sources_and_passages')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def hit_key(hit):
    return (hit.page_id, hit.url, hit.revision_id, hit.normalizer_version,
            hit.source_fingerprint, hit.document_fingerprint, hit.start, hit.end, hit.heading_path)


def source_key(excerpt):
    return excerpt.source, excerpt.revision_id


def metadata(hit):
    return dict(title=hit.title, page_id=hit.page_id, url=hit.url, revision_id=hit.revision_id,
                normalizer_version=hit.normalizer_version, source_fingerprint=hit.source_fingerprint,
                document_fingerprint=hit.document_fingerprint, start=hit.start, end=hit.end,
                heading_path=list(hit.heading_path))


def stable_semantic_order(items, score):
    """Python's stable sort retains the original rank on equal cosine scores."""
    return sorted(items, key=lambda item: -score(item))


def source_scores(admitted, scores):
    return {key: max(scores[hit_key(hit)] for hit in hits) for key, hits in admitted.items()}


class ObservedKnowledge(KnowledgeBase):
    """Question-local test spy; production helpers still execute every time."""

    def begin(self, *, frozen=None, mode='production', scores=None):
        self.trace = {'admitted': {}, 'focused': {}, 'ranked': []}
        self.frozen, self.mode, self.scores = frozen, mode, scores or {}

    def _research_gswiki(self, *, terms):
        result = super()._research_gswiki(terms=terms)
        self.trace['admitted'] = result[2]
        if self.frozen is not None:
            actual = [[hit_key(hit) for hit in hits] for hits in result[2].values()]
            expected = [[hit_key(hit) for hit in hits] for hits in self.frozen['admitted'].values()]
            if actual != expected:
                raise AssertionError('full-wiki candidate identity/order changed')
        return result

    def _research_document_passages(self, hit, excerpt, *, terms, limit=3):
        hits = super()._research_document_passages(hit, excerpt, terms=terms, limit=limit)
        key = source_key(excerpt)
        self.trace['focused'][key] = hits
        if self.frozen is not None and tuple(map(hit_key, hits)) != tuple(map(hit_key, self.frozen['focused'][key])):
            raise AssertionError('focused passage membership/order changed')
        if self.mode == 'semantic_sources_and_passages':
            return tuple(stable_semantic_order(hits, lambda item: self.scores[hit_key(item)]))
        return hits


def signature(record):
    return {'facts_found': sum(record['facts']), 'facts_total': len(record['facts']),
            'sources_found': record['sources_found'], 'served_titles': record['served_titles'],
            'reads': [{'status': read['status'],
                       **({key: read['data'][key] for key in ('title', 'start', 'end', 'complete')} |
                          {'revision_id': read['data']['provenance']['revision_id'],
                           'text_sha256': hashlib.sha256(read['data']['text'].encode()).hexdigest()}
                          if read['status'] == 'succeeded' else {})}
                      for read in record['read_evidence']]}


def run_case(knowledge, question, allowance, *, frozen=None, mode='production', scores=None):
    knowledge.begin(frozen=frozen, mode=mode, scores=scores)
    original_rank = research.rank_discovery

    def rank(candidates, terms):
        ranked = original_rank(candidates, terms)
        knowledge.trace['ranked'] = [source_key(item.excerpt) for item in ranked]
        if frozen is not None and knowledge.trace['ranked'] != frozen['ranked']:
            raise AssertionError('production candidate rank membership changed')
        if mode == 'production':
            return ranked
        # Only this question's original FTS-admitted ranges vote for its source.
        votes = source_scores(frozen['admitted'], knowledge.scores)
        return stable_semantic_order(ranked, lambda candidate: votes[source_key(candidate.excerpt)])

    session = knowledge.open_research(character='Example', max_chars=allowance)
    try:
        started = time.perf_counter()
        with patch.object(research, 'rank_discovery', rank):
            discovery = session.search(question['query'])
        search_ms = (time.perf_counter() - started) * 1000
        items = discovery['data']['items']
        started = time.perf_counter()
        reads = [session.read(source, section) for source, section in
                 semantic.lexical_benchmark.read_targets(items, 3)]
        read_ms = (time.perf_counter() - started) * 1000
        text = '\n'.join(read['data']['text'] for read in reads if read['status'] == 'succeeded').casefold()
        record = {'id': question['id'], 'category': question.get('category'), 'query_type': question.get('query_type'),
                  'search_ms': search_ms, 'read_ms': read_ms, 'pipeline_ms': search_ms + read_ms,
                  'facts': [fact.casefold() in text for fact in question['facts']],
                  'served_titles': [item['title'] for item in items],
                  'sources_found': sum(title in {item['title'] for item in items} for title in question['sources']),
                  'sources_total': len(question['sources']), 'read_evidence': reads,
                  'selected_passages': [{'title': item['title'], 'ranges': [
                      {key: section[key] for key in ('kind', 'start', 'end')} for section in item['sections']
                      if section['kind'] == 'passage']} for item in items],
                  'discovery_status': discovery['status']}
        return record, knowledge.trace
    finally:
        session.close()


def baseline_pass(knowledge, corpus, allowances):
    records, traces = {}, {}
    for allowance in allowances:
        records[str(allowance)] = {}
        for question in corpus['questions']:
            record, trace = run_case(knowledge, question, allowance)
            records[str(allowance)][question['id']] = record
            if question['id'] in traces:
                prior = traces[question['id']]
                if digest([[metadata(hit) for hit in hits] for hits in prior['admitted'].values()]) != digest(
                        [[metadata(hit) for hit in hits] for hits in trace['admitted'].values()]):
                    raise AssertionError('candidate set differs between allowances')
            else:
                traces[question['id']] = trace
    return records, traces


def validate_baselines(records, expected):
    actual = {allowance: {key: signature(record) for key, record in cases.items()}
              for allowance, cases in records.items()}
    expected = {allowance: value.get('cases', value) for allowance, value in expected.items()}
    if actual != expected:
        raise AssertionError('production baseline signatures differ; no inference permitted')
    return digest(actual)


def prepare_ranges(knowledge, corpus, traces):
    """Offline union cache only; query membership remains separately fenced."""
    started = time.perf_counter()
    passages, indexes, ledger, excerpts = [], {}, [], {}
    loaded_chars = 0
    focused_skipped = 0
    for question in corpus['questions']:
        trace = traces[question['id']]
        terms = tuple(term for term in _terms(question['query']) if term not in _CURATED_QUERY_NOISE) or _terms(question['query'])
        admitted_texts, focused_texts, candidates = [], [], []
        for key, admitted in trace['admitted'].items():
            if key not in excerpts:
                excerpts[key] = knowledge._load_research_document(admitted[0], retrieved_at=None)
                loaded_chars += len(excerpts[key].text)
            excerpt = excerpts[key]
            # Production loads an indexed candidate before _register rejects
            # oversized documents. Preserve that candidate and its admitted
            # ranges for source scoring; do not invent a focused recovery that
            # the actual search/read pipeline cannot reach.
            read_eligible = len(excerpt.text) <= research._MAX_DOCUMENT_CHARS
            if read_eligible:
                # No model, gold fact, or alternate query enters recovery.
                focused = KnowledgeBase._research_document_passages(knowledge, admitted[0], excerpt, terms=terms)
            else:
                focused = ()
                focused_skipped += 1
            if key in trace['focused'] and tuple(map(hit_key, trace['focused'][key])) != tuple(map(hit_key, focused)):
                raise AssertionError('offline focused recovery differs from baseline')
            trace['focused'][key] = focused
            for hits, target in ((admitted, admitted_texts), (focused, focused_texts)):
                for hit in hits:
                    body = excerpt.text[hit.start:hit.end]
                    target.append(body)
                    identity = hit_key(hit)
                    if identity not in indexes:
                        indexes[identity] = len(passages)
                        passages.append(dict(title=hit.title, heading_path=hit.heading_path,
                                             start=hit.start, end=hit.end, body=body))
            candidates.append({'title': excerpt.title, 'admitted': [metadata(hit) for hit in admitted],
                               'focused': [metadata(hit) for hit in focused], 'read_eligible': read_eligible,
                               'focused_skip_reason': None if read_eligible else 'production document character cap'})
        names = {candidate['title'] for candidate in candidates}
        ledger.append({'id': question['id'], 'candidates': candidates,
                       'candidate_sources_found': sum(title in names for title in question['sources']),
                       'candidate_sources_total': len(question['sources']),
                       'missing_required_sources': [title for title in question['sources'] if title not in names],
                       'admitted_fact_availability': [any(fact.casefold() in text.casefold() for text in admitted_texts)
                                                      for fact in question['facts']],
                       'focused_fact_availability': [any(fact.casefold() in text.casefold() for text in focused_texts)
                                                     for fact in question['facts']]})
    return passages, indexes, ledger, {'elapsed_ms': (time.perf_counter() - started) * 1000,
                                      'unique_snapshot_loads': len(excerpts), 'normalized_chars_loaded': loaded_chars,
                                      'unique_ranges': len(passages),
                                      'focused_skipped_query_sources': focused_skipped,
                                      'total_range_chars': sum(len(p['body']) for p in passages),
                                      'max_range_chars': max((len(p['body']) for p in passages), default=0)}


def benchmark(database, corpus, expected, encoder_factory, *, allowances=(6000, 10500)):
    with tempfile.TemporaryDirectory(prefix='lab-fullwiki-rerank-') as directory:
        # Frozen experiment clock, not an assertion of current source freshness.
        knowledge = ObservedKnowledge(wiki_root=Path(directory), gswiki_database=database,
            online_fallback=OnlineFallbackPolicy.DISABLED, now=lambda: datetime(2026, 9, 7, tzinfo=UTC))
        baselines, traces = baseline_pass(knowledge, corpus, allowances)
        baseline_hash = validate_baselines(baselines, expected)
        passages, indexes, ledger, preparation = prepare_ranges(knowledge, corpus, traces)
        encoder = encoder_factory()  # Deliberately after baseline verification.
        started = time.perf_counter()
        embedding = encoder.prepare(passages)
        embedding['elapsed_ms'] = (time.perf_counter() - started) * 1000
        report = {'corpus_sha256': digest(corpus), 'baseline_signatures_sha256': baseline_hash,
                  'candidate_ledger_sha256': digest(ledger), 'candidate_ledger': ledger,
                  'preparation': preparation, 'embedding': embedding, 'model': encoder.identity,
                  'read_budget': 3, 'max_sources': 6, 'discovery_limit': 36, 'results': {},
                  'limitations': ['Full-wiki lexical discovery remains unchanged; semantic order cannot recover missing sources.',
                      'Offline cache preparation loads the union of query-admitted snapshots and focused ranges. It is not request latency.',
                      'Over-cap documents retain their originally admitted scoring ranges but receive no focused recovery; genuine production registration still rejects them.',
                      'Per-query scores are fenced to that query\'s admitted and focused ranges; cached ranges from other queries never vote.',
                      'Semantic timing scores the offline union cache, then fences membership; not an optimized production reranker.',
                      'Timed production runs repeat after preparation and must match their pre-inference signatures. Modes run in fixed order once; residual cache/order effects remain.',
                      'Source-plus-passage order enters the unchanged coverage-window heuristic and may change its composed range.',
                      'Literal actual-read coverage is not generated-answer correctness or a representative player population.']}
        for allowance in allowances:
            records = []
            for question in corpus['questions']:
                trace = traces[question['id']]
                started = time.perf_counter()
                values = encoder.scores(question['query'])
                semantic_ms = (time.perf_counter() - started) * 1000
                allowed = {hit_key(hit) for group in ('admitted', 'focused') for hits in trace[group].values() for hit in hits}
                scores = {key: values[indexes[key]] for key in allowed}
                production, _ = run_case(knowledge, question, allowance, frozen=trace)
                if signature(production) != signature(baselines[str(allowance)][question['id']]):
                    raise AssertionError('warmed production result differs from verified baseline')
                modes = {'production': production}
                for mode in MODES[1:]:
                    record, _ = run_case(knowledge, question, allowance, frozen=trace, mode=mode, scores=scores)
                    record['semantic_ms'] = semantic_ms
                    record['pipeline_plus_semantic_ms'] = record['pipeline_ms'] + semantic_ms
                    record['fact_delta_from_production'] = sum(record['facts']) - sum(modes['production']['facts'])
                    modes[mode] = record
                records.append({'id': question['id'], 'modes': modes})
            report['results'][str(allowance)] = records
        report['peak_rss_kib_linux'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--expected-baselines', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    args = parser.parse_args()
    with (patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')),
          patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden'))):
        report = benchmark(args.database, json.loads(args.corpus.read_text()),
            json.loads(args.expected_baselines.read_text()), lambda: semantic.OnnxEncoder(args.model_dir))
    report['input_file_hashes'] = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in
                                  [('corpus', args.corpus), ('expected_baselines', args.expected_baselines)]}
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
