#!/usr/bin/env python3
"""Measure actual runtime semantic on/off behavior with frozen local questions.

No ranking hooks, injected answer sources, downloads, or live game operations.
Real-corpus reports contain private source metadata and must stay outside the repo.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
from lich_agent_bridge.knowledge import KnowledgeBase
from lich_agent_bridge.passage_index import status as index_status
from lich_agent_bridge.semantic import ARTIFACTS, MODEL_ID, SemanticReranker
from lich_agent_bridge.settings import OnlineFallbackPolicy

spec = importlib.util.spec_from_file_location('runtime_lexical_benchmark',
    Path(__file__).with_name('benchmark-wiki-retrieval.py'))
lexical = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lexical)
ALLOWANCES = (6000, 10500)
PASSES = ('first_pass', 'warm_pass')


@contextmanager
def offline():
    with (patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')),
          patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden'))):
        yield


class RecordingSession:
    """Forward the genuine session unchanged while recording observed results."""

    def __init__(self, session):
        self.session, self.discovery, self.reads = session, None, []

    def search(self, query):
        self.discovery = self.session.search(query)
        return self.discovery

    def read(self, *args, **kwargs):
        result = self.session.read(*args, **kwargs)
        self.reads.append(result)
        return result

    def close(self):
        self.session.close()


class RecordingKnowledge:
    """Only override the benchmark's fixed allowance, never source selection."""

    def __init__(self, knowledge, allowance):
        self.knowledge, self.allowance, self.recording = knowledge, allowance, None

    def open_research(self, *, character, max_chars=6000):
        self.recording = RecordingSession(self.knowledge.open_research(
            character=character, max_chars=self.allowance))
        return self.recording


def run_case(knowledge, question, allowance):
    observed = RecordingKnowledge(knowledge, allowance)
    metrics = lexical.run_question(observed, question, 3)
    trace = observed.recording
    successful_text = '\n'.join(result['data']['text'] for result in trace.reads
                                 if result['status'] == 'succeeded').casefold()
    facts = [fact.casefold() in successful_text for fact in question['facts']]
    assert sum(facts) == metrics['facts_found'], 'literal scoring differs from frozen reader'
    diagnostics = list(trace.discovery.get('diagnostics', []))
    reads = []
    for result in trace.reads:
        diagnostics.extend(result.get('diagnostics', []))
        data = result.get('data', {})
        text = data.get('text', '')
        reads.append({'status': result['status'],
            **{key: data[key] for key in ('title', 'start', 'end', 'complete', 'at_end') if key in data},
            'text_chars': len(text), 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'has_continuation': bool(data.get('next_cursor')),
            'sources': [{key: source[key] for key in ('title', 'url', 'revision_id', 'page_id',
                'normalizer_version', 'text_fingerprint', 'evidence_kind', 'kind', 'start', 'end',
                'complete', 'freshness') if key in source} for source in result.get('sources', [])]})
    return {**metrics, 'facts': facts, 'read_evidence': reads, 'diagnostics': diagnostics,
            'served_titles': [item['title'] for item in trace.discovery['data']['items']],
            'category': question.get('category'), 'query_type': question.get('query_type')}


def summarize(cases):
    statuses = Counter((str(d.get('source')), str(d.get('status'))) for case in cases
                       for d in case['diagnostics'] if 'semantic' in str(d.get('source', '')))
    return {'questions': len(cases), 'facts_found': sum(case['facts_found'] for case in cases),
            'facts_total': sum(case['facts_total'] for case in cases),
            'complete_questions': sum(case['complete_facts'] for case in cases),
            'sources_found': sum(case['sources_found'] for case in cases),
            'sources_total': sum(case['sources_total'] for case in cases),
            'total_ms': lexical.distribution([case['total_ms'] for case in cases]),
            'search_ms': lexical.distribution([case['search_ms'] for case in cases]),
            'read_ms': lexical.distribution([case['read_ms'] for case in cases]),
            'semantic_statuses': [{'source': source, 'status': status, 'count': count}
                                  for (source, status), count in sorted(statuses.items())]}


def benchmark(database, corpus, model_directory, *, reranker_factory=None, allowances=ALLOWANCES):
    questions = corpus.get('questions', [])
    if not questions or len({question['id'] for question in questions}) != len(questions):
        raise ValueError('questions must have unique IDs and cannot be empty')
    if not allowances or any(allowance not in ALLOWANCES for allowance in allowances):
        raise ValueError('only the frozen 6000/10500 allowances are supported')
    database = Path(database).resolve()
    if not database.is_file():
        raise ValueError('an existing copied indexed database is required')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
        if index_status(connection)['status'] != 'ready':
            raise ValueError('the copied passage index must be ready')
    report = {'model': {'id': MODEL_ID, 'required_artifacts': ARTIFACTS,
                       'injected_test_factory': reranker_factory is not None},
              'read_budget': 3, 'max_sources': 6, 'results': {},
              'limitations': [
                  'Uses actual KnowledgeBase/ResearchSession behavior; semantic failures retain runtime lexical fallback.',
                  'First pass starts with a fresh reranker and evolving cache; it is not an OS-cold or per-question-cold measurement.',
                  'Warm pass reuses the same bounded runtime cache; entries can be evicted, so not every window is guaranteed cached.',
                  'Modes run off then on in fixed order; filesystem caches are not reset. Timings are diagnostic, not controlled speed estimates.',
                  'Search timing includes lazy model loading, query/window encoding, runtime caps, ordering, and packing where performed.',
                  'Literal three-read coverage is not generated-answer correctness or population-level accuracy.',
                  'No wiki text is written to the report, but titles, provenance and questions remain private benchmark metadata.']}
    factory = reranker_factory or SemanticReranker
    with offline(), tempfile.TemporaryDirectory(prefix='lab-runtime-semantic-') as directory:
        for allowance in allowances:
            modes = {}
            for mode in ('off', 'on'):
                started = time.perf_counter()
                reranker = factory(model_directory) if mode == 'on' else None
                knowledge = KnowledgeBase(wiki_root=Path(directory), gswiki_database=database,
                    online_fallback=OnlineFallbackPolicy.DISABLED, semantic_reranker=reranker,
                    now=lambda: datetime(2026, 9, 7, tzinfo=UTC))
                results = {'construction_ms': (time.perf_counter() - started) * 1000}
                for pass_name in PASSES:
                    cases = [run_case(knowledge, question, allowance) for question in questions]
                    results[pass_name] = {'summary': summarize(cases), 'cases': cases}
                modes[mode] = results
            deltas = {}
            for pass_name in PASSES:
                comparisons = []
                for off, on in zip(modes['off'][pass_name]['cases'], modes['on'][pass_name]['cases']):
                    assert off['id'] == on['id']
                    comparisons.append({'id': off['id'], 'fact_delta': on['facts_found'] - off['facts_found'],
                        'gained_fact_indices': [i for i, (a, b) in enumerate(zip(off['facts'], on['facts'])) if b and not a],
                        'lost_fact_indices': [i for i, (a, b) in enumerate(zip(off['facts'], on['facts'])) if a and not b],
                        'total_ms_delta': on['total_ms'] - off['total_ms']})
                deltas[pass_name] = {'fact_delta': sum(case['fact_delta'] for case in comparisons),
                    'improved_questions': sum(case['fact_delta'] > 0 for case in comparisons),
                    'regressed_questions': sum(case['fact_delta'] < 0 for case in comparisons), 'cases': comparisons}
            report['results'][str(allowance)] = {'modes': modes, 'changes': deltas}
    return report


def file_hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def private_output(path):
    path = Path(path).resolve()
    if path.is_relative_to(REPO):
        raise ValueError('benchmark output must be outside the public repository')
    if path.exists():
        raise FileExistsError(path)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--questions', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--model-directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    output = private_output(args.output)
    hashes = {name: file_hash(path) for name, path in (
        ('questions', args.questions), ('database', args.database), ('harness', Path(__file__)))}
    sources = {str(path.relative_to(REPO)): file_hash(path)
               for path in (REPO / 'src' / 'lich_agent_bridge').glob('*.py')}
    report = benchmark(args.database, json.loads(args.questions.read_text()), args.model_directory)
    report['input_file_hashes'] = hashes
    report['runtime_source_hashes'] = sources
    if hashes['questions'] != file_hash(args.questions) or hashes['database'] != file_hash(args.database):
        raise RuntimeError('benchmark inputs changed during the run; refusing to publish results')
    if any(file_hash(REPO / path) != digest for path, digest in sources.items()):
        raise RuntimeError('runtime sources changed during the run; refusing to publish results')
    with output.open('x', encoding='utf-8') as destination:
        json.dump(report, destination, indent=2)
        destination.write('\n')
    print(json.dumps({'output': str(output), 'input_file_hashes': hashes}))


if __name__ == '__main__':
    main()
