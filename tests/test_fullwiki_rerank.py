"""No model quality claims: test the optional full-pipeline experiment's fences."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch


def module():
    path = Path(__file__).resolve().parents[1] / 'scripts/benchmark-fullwiki-rerank.py'
    spec = importlib.util.spec_from_file_location('fullwiki_rerank', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class FakeEncoder:
    identity = {'model': 'deterministic orchestration fixture, not semantic quality'}

    def prepare(self, passages):
        self.count = len(passages)
        self.inputs = passages
        return {'windows': self.count}

    def scores(self, query):
        return [float(index) for index in range(self.count)]


class FullWikiRerankTests(unittest.TestCase):
    def setUp(self):
        self.b = module()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'wiki.sqlite3'
        self.corpus = {'pages': [
            {'title': 'Cobalt manual', 'text': '== Maintenance ==\nCobalt quartz treatment lasts seven beats.\n'
             '== Repair ==\nCobalt quartz repair requires three gears.\n'
             '== Storage ==\nCobalt quartz storage uses lined boxes.'},
            {'title': 'Quartz manual', 'text': '== Maintenance ==\nQuartz cobalt treatment lasts nine beats.'},
            {'title': 'Unrelated handbook', 'text': 'This chapter describes painting.'}],
            'questions': [{'id': 'fixture', 'query': 'cobalt quartz maintenance repair',
                           'sources': ['Cobalt manual'], 'facts': ['seven beats', 'three gears']}]}
        self.b.semantic.lexical_benchmark.create_mirror(self.database, self.corpus)
        from lich_agent_bridge.passage_index import build_index
        with sqlite3.connect(self.database) as connection:
            build_index(connection)
        self.knowledge = self.b.ObservedKnowledge(wiki_root=self.root / 'empty', gswiki_database=self.database)

    def freeze(self):
        records, traces = self.b.baseline_pass(self.knowledge, self.corpus, (6000,))
        expected = {'6000': {key: self.b.signature(value) for key, value in records['6000'].items()}}
        passages, indexes, ledger, report = self.b.prepare_ranges(self.knowledge, self.corpus, traces)
        return records, traces, expected, passages, indexes, ledger, report

    def test_baseline_matches_unmodified_research_search_and_reads(self):
        question = self.corpus['questions'][0]
        actual, _ = self.b.run_case(self.knowledge, question, 6000)
        session = self.knowledge.open_research(character='Example', max_chars=6000)
        try:
            search = session.search(question['query'])
            reads = [session.read(source, section) for source, section in
                     self.b.semantic.lexical_benchmark.read_targets(search['data']['items'], 3)]
            self.assertEqual(actual['served_titles'], [item['title'] for item in search['data']['items']])
            expected = {**actual, 'read_evidence': reads}
            self.assertEqual(self.b.signature(actual), self.b.signature(expected))
        finally:
            session.close()

    def test_failed_baseline_stops_before_encoder_construction(self):
        factory = Mock(side_effect=AssertionError('model should never start'))
        with self.assertRaisesRegex(AssertionError, 'baseline signatures differ'):
            self.b.benchmark(self.database, self.corpus, {}, factory, allowances=(6000,))
        factory.assert_not_called()

    def test_source_vote_ignores_other_query_ranges_and_stable_ties(self):
        _, traces, _, _, _, _, _ = self.freeze()
        admitted = traces['fixture']['admitted']
        scores = {self.b.hit_key(hit): 0.25 for hits in admitted.values() for hit in hits}
        first = next(iter(admitted.values()))[0]
        # Same page and snapshot, but a different range not admitted for this query.
        other_query_hit = replace(first, start=first.end + 100, end=first.end + 200)
        scores[self.b.hit_key(other_query_hit)] = 10000
        self.assertEqual(set(self.b.source_scores(admitted, scores).values()), {0.25})
        self.assertEqual(self.b.stable_semantic_order(['a', 'b'], lambda _: 0.25), ['a', 'b'])

    def test_source_only_preserves_focused_order_and_combined_only_permutes(self):
        _, traces, _, _, _, _, _ = self.freeze()
        frozen = traces['fixture']
        scores = {self.b.hit_key(hit): float(index) for group in ('admitted', 'focused')
                  for hits in frozen[group].values() for index, hit in enumerate(hits)}
        for mode in self.b.MODES[1:]:
            record, trace = self.b.run_case(self.knowledge, self.corpus['questions'][0], 6000,
                                          frozen=frozen, mode=mode, scores=scores)
            self.assertLessEqual(len(record['served_titles']), 6)
            self.assertLessEqual(len(record['read_evidence']), 3)
            for key, hits in trace['focused'].items():
                self.assertEqual(tuple(map(self.b.hit_key, hits)), tuple(map(self.b.hit_key, frozen['focused'][key])))
            # The hook must return only those same helper-selected ranges.
            for key, hits in frozen['focused'].items():
                admitted = frozen['admitted'][key]
                excerpt = self.knowledge._load_research_document(admitted[0], retrieved_at=None)
                terms = tuple(term for term in self.b._terms(self.corpus['questions'][0]['query'])
                              if term not in self.b._CURATED_QUERY_NOISE)
                returned = self.knowledge._research_document_passages(admitted[0], excerpt, terms=terms)
                expected = list(hits) if mode == 'semantic_sources' else self.b.stable_semantic_order(
                    hits, lambda hit: scores[self.b.hit_key(hit)])
                self.assertEqual(list(returned), expected)

    def test_changed_membership_fails_closed(self):
        _, traces, _, _, _, _, _ = self.freeze()
        frozen = {**traces['fixture'], 'admitted': {}}
        with self.assertRaisesRegex(AssertionError, 'candidate identity/order changed'):
            self.b.run_case(self.knowledge, self.corpus['questions'][0], 6000, frozen=frozen)

    def test_over_cap_candidate_remains_scored_but_is_never_focused_or_read(self):
        from lich_agent_bridge.gswiki import _upsert_page
        from lich_agent_bridge.passage_index import build_index
        title = 'Cobalt quartz oversized handbook'
        with sqlite3.connect(self.database) as connection:
            _upsert_page(connection, {'pageid': 99, 'ns': 0, 'title': title,
                'revisions': [{'revid': 999, 'slots': {'main': {'content':
                    '== Maintenance ==\nCobalt quartz maintenance repair. ' + 'Background prose. ' * 100}}}]},
                '2026-09-06T00:00:00+00:00')
            build_index(connection)
        original = self.b.KnowledgeBase._research_document_passages

        def forbid_oversized(instance, hit, excerpt, **kwargs):
            self.assertNotEqual(excerpt.title, title)
            return original(instance, hit, excerpt, **kwargs)

        with (patch.object(self.b.research, '_MAX_DOCUMENT_CHARS', 500),
              patch.object(self.b.KnowledgeBase, '_research_document_passages', forbid_oversized)):
            _, traces, _, _, indexes, ledger, preparation = self.freeze()
            candidate = next(item for item in ledger[0]['candidates'] if item['title'] == title)
            self.assertFalse(candidate['read_eligible'])
            self.assertEqual(candidate['focused_skip_reason'], 'production document character cap')
            self.assertEqual(candidate['focused'], [])
            self.assertTrue(candidate['admitted'])
            self.assertEqual(preparation['focused_skipped_query_sources'], 1)
            frozen = traces['fixture']
            scores = {identity: 0.0 for identity in indexes}
            for hits in frozen['admitted'].values():
                for hit in hits:
                    if hit.title == title:
                        self.assertIn(self.b.hit_key(hit), indexes)
                        scores[self.b.hit_key(hit)] = 1000.0
            record, _ = self.b.run_case(self.knowledge, self.corpus['questions'][0], 6000,
                frozen=frozen, mode='semantic_sources_and_passages', scores=scores)
            self.assertNotIn(title, record['served_titles'])
            self.assertTrue(all(read['data']['title'] != title for read in record['read_evidence']))

    def test_end_to_end_fake_model_has_no_gold_inputs_network_or_database_writes(self):
        _, _, expected, _, _, _, _ = self.freeze()
        original = self.database.read_bytes()
        encoder = FakeEncoder()
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('offline only')):
            report = self.b.benchmark(self.database, self.corpus, expected, lambda: encoder, allowances=(6000,))
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(set(report['results']['6000'][0]['modes']), set(self.b.MODES))
        self.assertEqual(report['candidate_ledger'][0]['candidate_sources_found'], 1)
        self.assertTrue(all(set(item) == {'title', 'heading_path', 'start', 'end', 'body'} for item in encoder.inputs))
        self.assertNotIn('Unrelated handbook', {item['title'] for item in encoder.inputs})
        self.assertGreater(report['preparation']['max_range_chars'], 0)


if __name__ == '__main__':
    unittest.main()
