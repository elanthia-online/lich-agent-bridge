"""Synthetic boundary checks for merged, revision-bound passage reads."""

import sqlite3
import unittest
from unittest.mock import Mock

from lich_agent_bridge.knowledge import KnowledgeExcerpt, KnowledgeSourceDiagnostic
from lich_agent_bridge.passage_index import build_index
from lich_agent_bridge.settings import OnlineFallbackPolicy
from . import test_research_sources as fixtures


class ResearchCoverageBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResearchSourceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def indexed(self, content, **options):
        self.fixture.mirror(content, stamp='2020-01-01T00:00:00+00:00')
        with sqlite3.connect(self.fixture.database) as connection:
            build_index(connection)
        session = self.fixture.knowledge(**options).open_research(character='Example')
        self.addCleanup(session.close)
        item = session.search('cobalt duration and quartz capacity')['data']['items'][0]
        passage = next(s for s in item['sections'] if s.get('kind') == 'passage')
        return session, item, passage

    def test_merged_handle_rejects_changed_revision_without_heading_remap(self):
        live = Mock()
        live.read.return_value = (
            KnowledgeExcerpt('live GSWiki API', 'Synthetic Equipment',
                '## Duration\nCobalt duration is new.\n## Capacity\nQuartz capacity is new.',
                'https://gswiki.play.net/Synthetic_Equipment', revision_id=8),
            KnowledgeSourceDiagnostic('live_gswiki', 'success', 'synthetic refresh'))
        session, item, passage = self.indexed(
            '== Duration ==\nCobalt duration is eleven beats.\n'
            '== Capacity ==\nQuartz capacity is seven units.',
            live_gswiki=live, online_fallback=OnlineFallbackPolicy.WHEN_NEEDED)
        self.assertEqual(passage['heading_path'], [])
        result = session.read(item['source_id'], passage['section_id'])
        self.assertEqual(result['status'], 'revision_changed')
        self.assertNotIn('text', result['data'])
        self.assertEqual(result['sources'], [])

    def test_merged_read_marks_intervening_oversized_protected_table(self):
        table = '{| class="wikitable"\n! Material !! Rule\n|-\n| Example || ' + ('ordinary ' * 230) + '\n|}'
        session, item, passage = self.indexed(
            '== Duration ==\nCobalt duration is eleven beats.\n'
            '== Limits ==\nThe following qualification applies.\n\n' + table +
            '\n\nOnly stationary equipment qualifies.\n'
            '== Capacity ==\nQuartz capacity is seven units.')
        result = session.read(item['source_id'], passage['section_id'])
        self.assertIn('eleven beats', result['data']['text'])
        self.assertIn('seven units', result['data']['text'])
        self.assertIn(table, result['data']['text'])
        self.assertIn('Only stationary equipment qualifies.', result['data']['text'])
        self.assertTrue(passage['oversized_structure'])


if __name__ == '__main__':
    unittest.main()
