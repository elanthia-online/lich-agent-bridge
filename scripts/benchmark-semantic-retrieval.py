#!/usr/bin/env python3
"""Optional offline subset experiment, not LAB's production retrieval provider.

Requires local numpy, tokenizers and onnxruntime only when running the CLI.
Reads a frozen corpus and explicit page manifest; never downloads anything.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lich_agent_bridge.knowledge import KnowledgeBase, KnowledgeExcerpt, _terms, _CURATED_QUERY_NOISE
from lich_agent_bridge.passage_index import PassageHit, _check, load_document
from lich_agent_bridge.settings import OnlineFallbackPolicy

spec = importlib.util.spec_from_file_location('lexical_benchmark', Path(__file__).with_name('benchmark-wiki-retrieval.py'))
lexical_benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lexical_benchmark)


def token_windows(ids, offsets, limit, overlap=32):
    """Cover every token, with offsets against the unchanged parent text."""
    if limit <= overlap or len(ids) != len(offsets):
        raise ValueError('invalid token window bounds')
    start = 0
    while start < len(ids):
        end = min(start + limit, len(ids))
        yield ids[start:end], offsets[start][0], offsets[end - 1][1]
        if end == len(ids):
            break
        start = end - overlap


class OnnxEncoder:
    """The one experiment model: local MiniLM masked-mean, L2 embeddings."""

    def __init__(self, directory, *, threads=2):
        started = time.perf_counter()
        if not 1 <= threads <= 16:
            raise ValueError('threads must be 1..16')
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        ort.disable_telemetry_events()
        self.np = np
        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        model = directory / 'model.onnx'
        if not model.is_file():
            model = directory / 'onnx/model.onnx'
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model), sess_options=options, providers=['CPUExecutionProvider'])
        self.cls = self.tokenizer.token_to_id('[CLS]')
        self.sep = self.tokenizer.token_to_id('[SEP]')
        self.pad = self.tokenizer.token_to_id('[PAD]')
        if None in (self.cls, self.sep, self.pad):
            raise ValueError('expected MiniLM special tokens')
        self.identity = {'model': 'sentence-transformers/all-MiniLM-L6-v2',
            'artifacts': {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in (model, directory / 'tokenizer.json')},
            'pooling': 'attention-masked mean, L2 normalized', 'dimensions': 384,
            'max_tokens': 256, 'threads': threads, 'onnxruntime': ort.__version__,
            'load_ms': (time.perf_counter() - started) * 1000}

    def _encode_ids(self, sequences):
        np = self.np
        vectors = []
        for begin in range(0, len(sequences), 32):
            batch = sequences[begin:begin + 32]
            width = max(map(len, batch))
            ids = np.full((len(batch), width), self.pad, dtype=np.int64)
            mask = np.zeros_like(ids)
            for row, tokens in enumerate(batch):
                ids[row, :len(tokens)] = tokens
                mask[row, :len(tokens)] = 1
            values = {'input_ids': ids, 'attention_mask': mask, 'token_type_ids': np.zeros_like(ids)}
            output = self.session.run(None, {item.name: values[item.name] for item in self.session.get_inputs()})[0]
            if output.ndim != 3 or output.shape[-1] != 384:
                raise ValueError('expected 384-dimensional token embeddings')
            weights = mask[..., None].astype(np.float32)
            pooled = (output * weights).sum(axis=1) / weights.sum(axis=1).clip(min=1)
            vectors.append(pooled / np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-12))
        return np.concatenate(vectors) if vectors else np.empty((0, 384), dtype=np.float32)

    def prepare(self, passages, *, max_windows=50000):
        windows, sequences = [], []
        for index, passage in enumerate(passages):
            prefix = self.tokenizer.encode(passage['title'] + '\n' + ' / '.join(passage['heading_path']),
                                           add_special_tokens=False).ids[:64]
            body = self.tokenizer.encode(passage['body'], add_special_tokens=False)
            for ids, start, end in token_windows(body.ids, body.offsets, 254 - len(prefix)):
                windows.append((index, passage['start'] + start, passage['start'] + end))
                sequences.append([self.cls, *prefix, *ids, self.sep])
                if len(windows) > max_windows:
                    raise ValueError('subset exceeds window cap; choose a smaller frozen subset')
        self.windows = windows
        self.vectors = self._encode_ids(sequences)
        self.count = len(passages)
        return {'windows': len(windows), 'vector_bytes': self.vectors.nbytes,
                'window_overlap_tokens': 32, 'prefix_max_tokens': 64,
                'window_ranges_sha256': hashlib.sha256(json.dumps(windows).encode()).hexdigest()}

    def scores(self, query):
        ids = self.tokenizer.encode(query, add_special_tokens=False).ids
        if len(ids) > 254:
            raise ValueError('query exceeds model window; query truncation is not allowed')
        vector = self._encode_ids([[self.cls, *ids, self.sep]])[0]
        similarities = self.vectors @ vector
        scores = [-1.0] * self.count
        for (index, _, _), score in zip(self.windows, similarities):
            scores[index] = max(scores[index], float(score))
        return scores


def subset(connection, titles):
    _check(connection)
    passages, documents, skipped = [], {}, []
    for title in dict.fromkeys(titles):
        row = connection.execute('SELECT page_id, namespace, is_redirect, deprecated, length(text) '
                                 'FROM wiki_documents WHERE title=?', (title,)).fetchone()
        reason = ('missing' if row is None else 'ineligible source' if row[1] not in (0, 4) or row[2] or row[3]
                  else 'document exceeds 1000000 chars' if row[4] > 1_000_000 else None)
        if reason:
            skipped.append({'title': title, 'reason': reason})
            continue
        document = load_document(connection, row[0])
        documents[document.page_id] = document
        for record in connection.execute('SELECT passage_id,start,end,heading_path,body,oversized_structure '
                                         'FROM wiki_passages WHERE page_id=? ORDER BY start', (document.page_id,)):
            passage_id, start, end, headings, body, oversized = record
            if body != document.text[start:end]:
                raise ValueError('passage differs from its normalized snapshot')
            passages.append(dict(passage_id=passage_id, page_id=document.page_id, title=title,
                                 start=start, end=end, heading_path=json.loads(headings), body=body,
                                 oversized_structure=bool(oversized)))
    return documents, passages, skipped


def lexical_order(connection, query, passages, limit):
    terms = tuple(term for term in _terms(query) if term not in _CURATED_QUERY_NOISE) or _terms(query)
    if not terms or not passages:
        return []
    match = ' OR '.join('"' + term.replace('"', '""') + '"*' for term in terms[:10])
    pages = sorted({passage['page_id'] for passage in passages})
    indexes = {passage['passage_id']: index for index, passage in enumerate(passages)}
    rows = connection.execute('SELECT p.passage_id FROM wiki_passages_fts '
        'JOIN wiki_passages p ON p.passage_id=wiki_passages_fts.rowid '
        f'WHERE wiki_passages_fts MATCH ? AND p.page_id IN ({",".join("?" for _ in pages)}) '
        "AND wiki_passages_fts.rank MATCH 'bm25(3.0, 1.0)' ORDER BY wiki_passages_fts.rank LIMIT ?",
        (match, *pages, limit))
    return [indexes[row[0]] for row in rows]


def compare_orders(lexical, scores, limit):
    semantic = sorted(range(len(scores)), key=lambda index: (-scores[index], index))[:limit]
    fused = defaultdict(float)
    for lane in (lexical, semantic):
        for rank, index in enumerate(lane, 1):
            fused[index] += 1 / (60 + rank)
    return {'subset_bm25': lexical,
            'semantic_union_rrf': sorted(fused, key=lambda index: (-fused[index], index))[:limit],
            'semantic_rerank_lexical': sorted(lexical, key=lambda index: (-scores[index], index))}


def read_order(knowledge, documents, passages, order, query, max_chars):
    """Experiment shortlist, genuine registered snapshots/packing/three reads."""
    session = knowledge.open_research(character='Example', max_chars=max_chars)
    try:
        groups = {}
        for index in order:
            passage = passages[index]
            if passage['page_id'] not in groups and len(groups) == 6:
                continue
            group = groups.setdefault(passage['page_id'], [])
            if len(group) < 3:
                group.append(passage)
        prepared = []
        terms = _terms(query)
        for page_id, group in groups.items():
            snapshot = documents[page_id]
            excerpt = KnowledgeExcerpt('external GSWiki reference', snapshot.title, snapshot.text,
                snapshot.url, url=snapshot.url, revision_id=snapshot.revision_id)
            document = session._register(excerpt, freshness='stale', scope='reference', publish=False)
            hits = [PassageHit(page_id=page_id, title=snapshot.title, url=snapshot.url,
                revision_id=snapshot.revision_id, namespace=snapshot.namespace,
                normalizer_version=snapshot.normalizer_version, source_fingerprint=snapshot.source_fingerprint,
                document_fingerprint=snapshot.document_fingerprint, start=p['start'], end=p['end'],
                text=p['body'][:1800], text_end=p['start'] + min(1800, len(p['body'])),
                heading_path=tuple(p['heading_path']), oversized_structure=p['oversized_structure'], rank=0.0)
                for p in group]
            handles = session._register_passages(document, hits)
            prepared.append((document, session._candidate(document, terms, handles),
                             session._source(document, 'discovery')))
        items, _, selected, omitted = session._pack_discovery(prepared, terms)
        for document in selected:
            session._documents[document.source_id] = document
            session._identities[session._identity(document.scope, document.excerpt, document.revision)] = document.source_id
        readings = [session.read(source, section) for source, section in lexical_benchmark.read_targets(items, 3)]
        return items, readings, omitted
    finally:
        session.close()


def benchmark(database, corpus, titles, encoder, *, max_chars=6000, pool=36):
    if not 256 <= max_chars <= 10500 or not 1 <= pool <= 72 or not 1 <= len(titles) <= 256:
        raise ValueError('invalid pilot envelope')
    started = time.perf_counter()
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        documents, passages, skipped = subset(connection, titles)
        if not passages or len(passages) > 20000:
            raise ValueError('subset must contain 1..20000 passages')
        build = encoder.prepare(passages)
        build['elapsed_ms'] = (time.perf_counter() - started) * 1000
        report = {'model': encoder.identity, 'build': build, 'documents': len(documents),
            'passages': len(passages), 'skipped': skipped, 'max_chars': max_chars, 'pool': pool,
            'snapshot_sha256': hashlib.sha256(json.dumps(sorted((document.title, document.revision_id,
                document.document_fingerprint) for document in documents.values())).encode()).hexdigest(),
            'read_budget': 3, 'cases': [],
            'limitations': ['Subset BM25 is not production PIF: no title lane, page recovery, or production source ranker.',
                'A target-plus-distractor manifest deliberately includes answer sources; this is not full-wiki recall.',
                'All modes use the same experiment source shortlist and genuine ResearchSession packing/reads.',
                'Window similarity is max-pooled to original passage ranges; long protected ranges may need unread continuation.',
                'Literal facts can miss equivalent wording. A deterministic encoder tests plumbing, not semantic quality.',
                'Timings include all-subset semantic scoring even for lexical-pool reranking; no optimized reranker speed claim.']}
        with tempfile.TemporaryDirectory(prefix='lab-semantic-empty-') as directory:
            knowledge = KnowledgeBase(wiki_root=Path(directory), gswiki_database=database,
                                      online_fallback=OnlineFallbackPolicy.DISABLED)
            for question in corpus['questions']:
                start = time.perf_counter()
                lexical = lexical_order(connection, question['query'], passages, pool)
                lexical_ms = (time.perf_counter() - start) * 1000
                start = time.perf_counter()
                scores = encoder.scores(question['query'])
                semantic_ms = (time.perf_counter() - start) * 1000
                case = {'id': question['id'], 'category': question.get('category'),
                        'query_type': question.get('query_type'), 'lexical_ms': lexical_ms,
                        'semantic_ms': semantic_ms, 'missing_required_sources': sorted(set(question['sources'])
                            - {document.title for document in documents.values()}),
                        'revision_mismatches': [evidence['title'] for evidence in question.get('evidence', [])
                            if any(document.title == evidence['title'] and document.revision_id != evidence['revision_id']
                                   for document in documents.values())], 'modes': {}}
                for name, order in compare_orders(lexical, scores, pool).items():
                    start = time.perf_counter()
                    items, readings, omitted = read_order(knowledge, documents, passages, order, question['query'], max_chars)
                    read_ms = (time.perf_counter() - start) * 1000
                    text = '\n'.join(read['data']['text'] for read in readings if read['status'] == 'succeeded').casefold()
                    case['modes'][name] = {'facts_found': sum(fact.casefold() in text for fact in question['facts']),
                        'facts_total': len(question['facts']), 'reads': len(readings), 'read_ms': read_ms,
                        'incomplete_reads': sum(not read['data']['complete'] for read in readings if read['status'] == 'succeeded'),
                        'sources_found': sum(title in {item['title'] for item in items} for title in question['sources']),
                        'sources_total': len(question['sources']), 'omitted_sources': omitted,
                        'source_recall_at_k': {str(k): sum(title in {passages[i]['title'] for i in order[:k]}
                            for title in question['sources']) / len(question['sources']) for k in (3, 6, pool)}}
                report['cases'].append(case)
        report['peak_rss_kib_linux'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--max-chars', type=int, default=6000)
    parser.add_argument('--threads', type=int, default=2)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    titles = manifest if isinstance(manifest, list) else manifest['titles']
    report = benchmark(args.database, json.loads(args.corpus.read_text()), titles,
                       OnnxEncoder(args.model_dir, threads=args.threads), max_chars=args.max_chars)
    report['inputs'] = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for name, path in [('corpus', args.corpus), ('manifest', args.manifest)]}
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
