"""Actual-read coverage of separate clauses without increasing read budgets."""
import sqlite3
import unittest
from . import test_research_sources as fixtures
from lich_agent_bridge.passage_index import build_index


class ResearchCoverageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResearchSourceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def read_first(self, text, query, max_chars=6000):
        self.fixture.mirror(text)
        with sqlite3.connect(self.fixture.database) as connection:
            build_index(connection)
        session = self.fixture.knowledge().open_research(character='Example', max_chars=max_chars)
        self.addCleanup(session.close)
        item = session.search(query)['data']['items'][0]
        first = next(s for s in item['sections'] if s['kind']=='passage')
        return session.read(item['source_id'], first['section_id'])

    def test_first_read_covers_location_and_training_in_adjacent_sections(self):
        text = ('== Location ==\nCobalt generation requires outdoor ground.\n'
                '== Training ==\nQuartz training lets the caster choose the result.')
        result = self.read_first(text, 'cobalt generation location and quartz training choose result')
        self.assertIn('requires outdoor ground', result['data']['text'])
        self.assertIn('Quartz training lets', result['data']['text'])
        self.assertTrue(result['data']['complete'])

    def test_merged_read_remains_contiguous_and_preserves_middle_qualifier(self):
        text = ('== Duration ==\nCobalt duration is eleven beats.\n'
                '== Restrictions ==\nThese effects apply only to stationary equipment.\n'
                '== Capacity ==\nQuartz capacity is seven units.')
        result = self.read_first(text, 'cobalt duration and quartz capacity')
        self.assertIn('eleven beats', result['data']['text'])
        self.assertIn('only to stationary equipment', result['data']['text'])
        self.assertIn('seven units', result['data']['text'])
        self.assertEqual(result['data']['end']-result['data']['start'], len(result['data']['text']))

    def test_distant_match_does_not_expand_beyond_current_read_allowance(self):
        text = ('== Duration ==\nCobalt duration is eleven beats.\n'
                '== History ==\n' + 'Ordinary background.\n\n'*400 +
                '== Capacity ==\nQuartz capacity is seven units.')
        result = self.read_first(text, 'cobalt duration and quartz capacity')
        self.assertLessEqual(len(result['data']['text']), 5000)
        self.assertFalse('eleven beats' in result['data']['text'] and 'seven units' in result['data']['text'])
