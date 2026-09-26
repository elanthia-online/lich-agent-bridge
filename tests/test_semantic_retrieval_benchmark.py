"""Offline orchestration tests, not evidence of embedding-model quality."""
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from lich_agent_bridge.knowledge import KnowledgeBase
from lich_agent_bridge.passage_index import build_index
from lich_agent_bridge.settings import OnlineFallbackPolicy


def module():
    path = Path(__file__).resolve().parents[1] / 'scripts/benchmark-semantic-retrieval.py'
    spec = importlib.util.spec_from_file_location('semantic_benchmark', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class DeterministicEncoder:
    identity = {'model': 'synthetic fixed-order encoder; no semantic-quality claim'}

    def prepare(self, passages):
        self.count = len(passages)
        return {'windows': len(passages), 'vector_bytes': 0}

    def scores(self, query):
        return list(reversed(range(self.count)))


class SemanticRetrievalBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.benchmark = module()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'index.sqlite3'
        self.corpus = {'pages': [
            {'title': 'Quartz tools', 'text': '== Quartz rules ==\nQuartz lasts seven beats.'},
            {'title': 'Cobalt tools', 'text': '== Cobalt rules ==\nCobalt lasts nine beats.'},
            {'title': 'Unrelated volume', 'text': '== Ordinary ==\nNothing matching.'}],
            'questions': [{'id': 'synthetic', 'query': 'quartz duration', 'sources': ['Quartz tools'],
                           'facts': ['Quartz lasts seven beats.']}]}
        self.benchmark.lexical_benchmark.create_mirror(self.database, self.corpus)
        with sqlite3.connect(self.database) as connection:
            build_index(connection)

    def test_windows_cover_late_tokens_and_keep_character_offsets(self):
        ids = list(range(700))
        offsets = [(index * 4, index * 4 + 3) for index in ids]
        windows = list(self.benchmark.token_windows(ids, offsets, 190))
        self.assertEqual(set(token for tokens, _, _ in windows for token in tokens), set(ids))
        self.assertEqual(windows[-1][2], offsets[-1][1])
        self.assertTrue(all(len(tokens) <= 190 for tokens, _, _ in windows))
        self.assertTrue(all(left[0][-32:] == right[0][:32] for left, right in zip(windows, windows[1:])))
        with self.assertRaises(ValueError):
            list(self.benchmark.token_windows(ids, offsets, 32))

    def test_union_can_add_candidates_but_rerank_cannot(self):
        orders = self.benchmark.compare_orders([0], [0.1, 0.9], 2)
        self.assertEqual(orders['subset_bm25'], [0])
        self.assertEqual(orders['semantic_rerank_lexical'], [0])
        self.assertEqual(set(orders['semantic_union_rrf']), {0, 1})

    def test_subset_filters_before_lexical_limit_and_reports_missing_pages(self):
        with sqlite3.connect(self.database) as connection:
            documents, passages, skipped = self.benchmark.subset(connection, ['Cobalt tools', 'Missing'])
            order = self.benchmark.lexical_order(connection, 'quartz cobalt', passages, 1)
        self.assertEqual([passages[index]['title'] for index in order], ['Cobalt tools'])
        self.assertEqual(skipped, [{'title': 'Missing', 'reason': 'missing'}])
        self.assertEqual(len(documents), 1)

    def test_actual_reads_preserve_exact_snapshot_range_and_revision(self):
        with sqlite3.connect(self.database) as connection:
            documents, passages, _ = self.benchmark.subset(connection, ['Quartz tools'])
        knowledge = KnowledgeBase(wiki_root=self.root / 'empty', gswiki_database=self.database,
                                  online_fallback=OnlineFallbackPolicy.DISABLED)
        items, readings, omitted = self.benchmark.read_order(knowledge, documents, passages, [0], 'quartz', 6000)
        self.assertEqual(omitted, 0)
        self.assertEqual(len(items), 1)
        read = readings[0]['data']
        self.assertEqual(read['provenance']['revision_id'], 101)
        self.assertEqual(read['text'], documents[passages[0]['page_id']].text[read['start']:read['end']])
        self.assertIn('seven beats', read['text'])

    def test_benchmark_uses_no_network_or_required_model_packages_and_does_not_write_database(self):
        before = self.database.read_bytes()
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            report = self.benchmark.benchmark(self.database, self.corpus,
                [page['title'] for page in self.corpus['pages']], DeterministicEncoder())
        self.assertEqual(before, self.database.read_bytes())
        case = report['cases'][0]
        self.assertEqual(case['missing_required_sources'], [])
        self.assertEqual(set(case['modes']), {'subset_bm25', 'semantic_union_rrf', 'semantic_rerank_lexical'})
        self.assertEqual(case['modes']['subset_bm25']['facts_found'], 1)
        self.assertTrue(all(mode['reads'] <= 3 for mode in case['modes'].values()))
        self.assertIn('no semantic-quality claim', report['model']['model'])
        json.dumps(report)


if __name__ == '__main__':
    unittest.main()
