"""Actual runtime benchmark orchestration; fake scores make no quality claims."""

import hashlib
import importlib.util
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from lich_agent_bridge.passage_index import build_index
from lich_agent_bridge.semantic import SemanticScores, SemanticUnavailable


def module():
    path = Path(__file__).resolve().parents[1] / 'scripts/benchmark-runtime-semantic.py'
    spec = importlib.util.spec_from_file_location('runtime_semantic_benchmark', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class TieReranker:
    def __init__(self):
        self.calls = []

    def score(self, query, passages, *, check=lambda: None):
        check()
        passages = tuple(passages)
        self.calls.append((query, passages))
        return SemanticScores({passage.key: 0.25 for passage in passages}, {
            'windows': len(passages), 'cached_windows': 0,
            'encoded_windows': len(passages), 'total_ms': 0.0})


class RuntimeSemanticBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.b = module()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'mirror.sqlite3'
        self.corpus = {'pages': [
            {'title': 'Cobalt manual', 'text': '== Duration ==\nCobalt lasts seven beats.\n'
                '== Capacity ==\nQuartz capacity is three units.'},
            {'title': 'Unrelated source', 'text': 'Ordinary paint dries slowly.'}],
            'questions': [{'id': 'example', 'query': 'cobalt duration quartz capacity',
                'sources': ['Cobalt manual'], 'facts': ['seven beats', 'three units']}]}
        self.b.lexical.create_mirror(self.database, self.corpus)
        with sqlite3.connect(self.database) as connection:
            build_index(connection)

    def test_same_genuine_pipeline_and_reader_with_only_model_setting_changed(self):
        instances = []

        def factory(directory):
            self.assertEqual(directory, self.root / 'model')
            instance = TieReranker()
            instances.append(instance)
            return instance

        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        report = self.b.benchmark(self.database, self.corpus, self.root / 'model', reranker_factory=factory)
        self.assertEqual(len(instances), 2)
        self.assertTrue(all(instance.calls for instance in instances))
        self.assertTrue(report['model']['injected_test_factory'])
        for allowance in ('6000', '10500'):
            result = report['results'][allowance]
            for pass_name in self.b.PASSES:
                off = result['modes']['off'][pass_name]['cases'][0]
                on = result['modes']['on'][pass_name]['cases'][0]
                self.assertEqual(off['facts'], [True, True])
                self.assertEqual(on['facts'], off['facts'])
                self.assertEqual(on['served_titles'], off['served_titles'])
                self.assertEqual(on['read_evidence'], off['read_evidence'])
                self.assertLessEqual(len(on['read_evidence']), 3)
                self.assertEqual(result['changes'][pass_name]['fact_delta'], 0)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_missing_expected_sources_and_facts_never_enter_model_inputs(self):
        fake = TieReranker()
        corpus = {**self.corpus, 'questions': [{**self.corpus['questions'][0],
            'sources': ['GOLD_SOURCE_SENTINEL'], 'facts': ['GOLD_FACT_SENTINEL']}]}
        report = self.b.benchmark(self.database, corpus, self.root / 'model',
            allowances=(6000,), reranker_factory=lambda _: fake)
        self.assertTrue(fake.calls)
        for query, passages in fake.calls:
            self.assertEqual(query, corpus['questions'][0]['query'])
            self.assertNotIn('GOLD_', str(passages))
            self.assertNotIn('Unrelated source', {passage.title for passage in passages})
        case = report['results']['6000']['modes']['on']['warm_pass']['cases'][0]
        self.assertEqual(case['facts'], [False])
        self.assertEqual(case['sources_found'], 0)

    def test_runtime_failure_is_measured_as_fallback_not_hidden(self):
        fake = Mock()
        fake.score.side_effect = SemanticUnavailable('busy', 'Synthetic encoder busy.')
        report = self.b.benchmark(self.database, self.corpus, self.root / 'model',
            allowances=(6000,), reranker_factory=lambda _: fake)
        results = report['results']['6000']['modes']
        self.assertEqual(results['on']['warm_pass']['cases'][0]['facts'],
                         results['off']['warm_pass']['cases'][0]['facts'])
        self.assertIn('busy', str(results['on']['warm_pass']['cases'][0]['diagnostics']))
        self.assertTrue(results['on']['warm_pass']['summary']['semantic_statuses'])

    def test_allowance_adapter_forwards_to_real_open_research(self):
        knowledge = Mock()
        wrapper = self.b.RecordingKnowledge(knowledge, 10500)
        wrapper.open_research(character='Example', max_chars=6000)
        knowledge.open_research.assert_called_once_with(character='Example', max_chars=10500)

    def test_private_output_and_offline_guards(self):
        with self.assertRaises(ValueError):
            self.b.private_output(self.b.REPO / 'forbidden-results.json')
        self.assertEqual(self.b.private_output(self.root / 'results.json'), self.root / 'results.json')
        with self.assertRaises(FileExistsError):
            self.b.private_output(self.database)
        with self.b.offline(), self.assertRaisesRegex(AssertionError, 'network forbidden'):
            socket.create_connection(('example.invalid', 80))


if __name__ == '__main__':
    unittest.main()
