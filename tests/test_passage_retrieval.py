"""Synthetic passage selection through the real question-owned research seam."""
import hashlib
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lich_agent_bridge.context_assembler import ContextAssembler
from lich_agent_bridge.engine import Copilot
from lich_agent_bridge.evidence_tools import EvidenceTools
from lich_agent_bridge.gswiki import _upsert_page
from lich_agent_bridge.knowledge import KnowledgeExcerpt, KnowledgeSourceDiagnostic
from lich_agent_bridge.protocol import AskRequest
from lich_agent_bridge.settings import OnlineFallbackPolicy
from . import test_research_sources as fixtures
from . import test_discovery_ranking as ranking_fixtures
from .test_evidence_tools import Hub
from .test_evidence_integration import request
from .test_research_integration import records


class PassageRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResearchSourceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def indexed(self, content, **options):
        self.fixture.mirror(content, **options)
        self.build()
        return self.fixture.knowledge().open_research(character='Example')

    def build(self):
        from lich_agent_bridge.passage_index import build_index
        with sqlite3.connect(self.fixture.database) as connection:
            build_index(connection)

    @staticmethod
    def passages(item):
        return [section for section in item['sections'] if section.get('kind') == 'passage']

    def test_late_fact_under_generic_heading_is_directly_readable(self):
        session = self.indexed('== Details ==\n' + ('Unrelated descriptive material.\n\n' * 800)
                               + 'Cobalt gear retains half weighting when a tool is held.')
        item = session.search('cobalt weighting tool')['data']['items'][0]
        passage = next(s for s in item['sections'] if s.get('kind') == 'passage')
        result = session.read(item['source_id'], passage['section_id'])
        self.assertIn('retains half weighting', result['data']['text'])
        self.assertGreater(result['data']['start'], 10000)
        self.assertTrue(result['data']['complete'])
        self.assertEqual(result['sources'][0]['revision_id'], 7)
        self.assertIn('half weighting', item['snippet'])

    def test_distinct_facts_keep_nonoverlapping_passages_on_one_source(self):
        session = self.indexed('== Details ==\nCobalt retains half weighting.\n\n'
                               + 'Ordinary irrelevant material.\n\n' * 200
                               + 'Quartz duration is exactly seventeen beats.')
        items = session.search('cobalt weighting quartz duration')['data']['items']
        self.assertEqual(len(items), 1)
        passages = self.passages(items[0])
        self.assertGreaterEqual(len(passages), 2)
        reads = [session.read(items[0]['source_id'], passage['section_id']) for passage in passages]
        combined = '\n'.join(read['data']['text'] for read in reads)
        self.assertIn('half weighting', combined)
        self.assertIn('seventeen beats', combined)
        ranges = sorted((read['data']['start'], read['data']['end']) for read in reads)
        self.assertTrue(all(left[1] <= right[0] for left, right in zip(ranges, ranges[1:])))

    def test_repeated_search_adds_passages_without_invalidating_prior_handles(self):
        session = self.indexed('== Details ==\nCobalt retains half weighting.\n\n'
                               + 'Ordinary irrelevant material.\n\n' * 200
                               + 'Quartz duration is exactly seventeen beats.')
        first = session.search('cobalt weighting')['data']['items'][0]
        original = self.passages(first)[0]
        second = session.search('quartz duration')['data']['items'][0]
        self.assertEqual(first['source_id'], second['source_id'])
        self.assertNotEqual(original['section_id'], self.passages(second)[0]['section_id'])
        self.assertIn('half weighting', session.read(first['source_id'], original['section_id'])['data']['text'])
        third = session.search('cobalt weighting')['data']['items'][0]
        self.assertEqual(original['section_id'], self.passages(third)[0]['section_id'])
        foreign = self.fixture.knowledge().open_research(character='Example')
        with self.assertRaises(ValueError):
            foreign.read(first['source_id'], original['section_id'])

    def test_indexed_table_passage_keeps_headers_and_neighbor_qualifiers(self):
        table = '{| class="wikitable"\n! Equipment !! Weighting\n|-\n| Cobalt gloves || Half\n|}'
        session = self.indexed('== Details ==\n' + 'Unrelated prologue.\n\n' * 200
                               + 'Only hand attacks qualify.\n\n' + table + '\n\nKicks are excluded.')
        item = session.search('cobalt weighting')['data']['items'][0]
        read = session.read(item['source_id'], self.passages(item)[0]['section_id'])
        self.assertIn(table, read['data']['text'])
        self.assertIn('Only hand attacks qualify', read['data']['text'])
        self.assertIn('Kicks are excluded', read['data']['text'])

    def test_changed_revision_rejects_old_passage_even_with_same_unique_heading(self):
        self.indexed('== Details ==\nCobalt retains half weighting.', stamp='2020-01-01T00:00:00+00:00')
        live = Mock()
        live.read.return_value = (KnowledgeExcerpt('live GSWiki API', 'Synthetic Equipment',
            '## Details\nNew introductory material. Cobalt retains full weighting.',
            'https://gswiki.play.net/Synthetic_Equipment', revision_id=8),
            KnowledgeSourceDiagnostic('live_gswiki', 'success', 'synthetic refresh'))
        session = self.fixture.knowledge(online_fallback=OnlineFallbackPolicy.WHEN_NEEDED,
                                         live_gswiki=live).open_research(character='Example')
        item = session.search('cobalt weighting')['data']['items'][0]
        old = self.passages(item)[0]
        result = session.read(item['source_id'], old['section_id'])
        self.assertEqual(result['status'], 'revision_changed')
        self.assertNotIn('text', result['data'])
        self.assertEqual(result['sources'], [])
        replacement = result['data']['replacement']
        self.assertEqual(replacement['provenance']['revision_id'], 8)
        self.assertIn('full weighting', session.read(replacement['source_id'])['data']['text'])
        self.assertEqual(session.read(item['source_id'], old['section_id'])['status'], 'revision_changed')

    def test_missing_and_invalidated_index_use_read_only_legacy_with_online_disabled(self):
        self.fixture.mirror('== Weighting ==\nCobalt retains half weighting.')
        live = Mock()
        knowledge = self.fixture.knowledge(live_gswiki=live)
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                if indexed:
                    self.build()
                    self.fixture.mirror('== Weighting ==\nCobalt retains full weighting.', revision=8)
                before = hashlib.sha256(self.fixture.database.read_bytes()).hexdigest()
                session = knowledge.open_research(character='Example')
                result = session.search('cobalt weighting')
                item = result['data']['items'][0]
                self.assertFalse(self.passages(item))
                self.assertIn('legacy page search', str(result['diagnostics']))
                self.assertIn('weighting', session.read(item['source_id'])['data']['text'])
                self.assertEqual(hashlib.sha256(self.fixture.database.read_bytes()).hexdigest(), before)
        live.search.assert_not_called()
        live.read.assert_not_called()

    def test_indexed_discovery_never_reparses_wikitext_and_loads_at_most_six_sources(self):
        self.fixture.mirror('== Details ==\nCobalt retains half weighting.')
        with sqlite3.connect(self.fixture.database) as connection:
            for page_id in range(2, 31):
                _upsert_page(connection, {'pageid': page_id, 'ns': 0, 'title': f'Cobalt reference {page_id}',
                    'revisions': [{'revid': page_id, 'slots': {'main': {'content': '== Details ==\nCobalt weighting.'}}}]},
                    '2026-09-06T00:00:00+00:00')
        self.build()
        knowledge = self.fixture.knowledge()
        with patch('lich_agent_bridge.knowledge._research_text', side_effect=AssertionError('unexpected reparse')), \
             patch('lich_agent_bridge.wiki_text.research_text', side_effect=AssertionError('unexpected reparse')), \
             patch.object(knowledge, '_load_research_document', wraps=knowledge._load_research_document) as loader:
            result = knowledge.open_research(character='Example', max_chars=10500).search('cobalt weighting')
        self.assertLessEqual(loader.call_count, 6)
        self.assertTrue(result['data']['items'])
        self.assertLessEqual(len(result['data']['items']), 6)
        self.assertTrue(all(self.passages(item) for item in result['data']['items']))

    def test_corrupt_selected_passage_falls_back_without_issuing_its_range(self):
        session = self.indexed('== Details ==\nCobalt retains half weighting.')
        with sqlite3.connect(self.fixture.database) as connection:
            connection.execute("UPDATE wiki_passages SET body='Cobalt invented corrupt fact'")
        item_result = session.search('cobalt weighting')
        item = item_result['data']['items'][0]
        self.assertFalse(self.passages(item))
        self.assertIn('legacy page search', str(item_result['diagnostics']))
        read = session.read(item['source_id'])
        self.assertIn('half weighting', read['data']['text'])
        self.assertNotIn('invented corrupt fact', str(item_result))

    def test_outdated_normalizer_falls_back_without_mutating_the_index(self):
        session = self.indexed('== Details ==\nCobalt retains half weighting.')
        with sqlite3.connect(self.fixture.database) as connection:
            connection.execute("UPDATE passage_index_metadata SET value='0' WHERE key='normalizer_version'")
        before = hashlib.sha256(self.fixture.database.read_bytes()).hexdigest()
        result = session.search('cobalt weighting')
        self.assertFalse(self.passages(result['data']['items'][0]))
        self.assertIn('outdated', str(result['diagnostics']))
        self.assertEqual(hashlib.sha256(self.fixture.database.read_bytes()).hexdigest(), before)

    def test_selected_passages_reach_actual_answer_prompt_without_game_actions(self):
        self.indexed('== Details ==\n' + 'Unrelated prologue.\n\n' * 800
                     + 'COBALT-FACT: cobalt retains half weighting.')
        with sqlite3.connect(self.fixture.database) as connection:
            _upsert_page(connection, {'pageid': 2, 'ns': 0, 'title': 'Quartz mechanism',
                'revisions': [{'revid': 12, 'slots': {'main': {'content': '== Details ==\n'
                    + 'Ordinary material.\n\n' * 200
                    + 'QUARTZ-FACT: quartz duration is seventeen beats.'}}}]},
                '2026-09-06T00:00:00+00:00')
        self.build()
        knowledge = self.fixture.knowledge()
        hub = Hub()
        hub.knowledge = knowledge
        calls = []
        def respond(**options):
            calls.append(options)
            prompt = options['input_text']
            if len(calls) == 1:
                self.assertNotIn('COBALT-FACT', prompt)
                return json.dumps(request('knowledge.search', query='cobalt weighting quartz duration'))
            if len(calls) == 2:
                items = next(record['result']['data']['items'] for record in records(prompt)
                            if record['request']['tool'] == 'knowledge.search')
                self.assertEqual(len(items), 2)
                return json.dumps({'requests': [{'tool': 'knowledge.read', 'arguments': {
                    'source_id': item['source_id'], 'section_id': self.passages(item)[0]['section_id']}}
                    for item in items]})
            reads = [record['result'] for record in records(prompt) if record['request']['tool'] == 'knowledge.read']
            text = '\n'.join(read['data']['text'] for read in reads)
            self.assertIn('COBALT-FACT', text)
            self.assertIn('QUARTZ-FACT', text)
            self.assertEqual(len({read['data']['source_id'] for read in reads}), 2)
            self.assertTrue(all(read['sources'][0]['evidence_kind'] == 'read' for read in reads))
            self.assertTrue(all(read['sources'][0]['normalizer_version'] == 1 for read in reads))
            return json.dumps({'answer': 'Cobalt retains half weighting; quartz lasts seventeen beats.'})
        model = SimpleNamespace(backend='synthetic', configured=True, respond=respond, timing_metadata={})
        answer = Copilot(model, context_assembler=ContextAssembler(knowledge=knowledge),
                         evidence_tools=EvidenceTools(hub)).ask(AskRequest('Testmage', 'Cobalt and quartz rules?'))
        self.assertEqual(len(calls), 3)
        self.assertIn('seventeen beats', answer.text)
        self.assertEqual(hub.started, [])


class IndexedDiscoveryRankingTests(ranking_fixtures.DiscoveryRankingTests):
    """Run the existing exact-title, spell, shopping and archive cases indexed."""
    def mirror(self, pages):
        super().mirror(pages)
        from lich_agent_bridge.passage_index import build_index
        with sqlite3.connect(self.database) as connection:
            build_index(connection)


if __name__ == '__main__':
    unittest.main()
