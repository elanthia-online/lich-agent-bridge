"""Synthetic first-read regressions for bounded passage ordering."""

import sqlite3
import unittest

from lich_agent_bridge.passage_index import build_index, query_passages
from . import test_research_sources as fixtures


class PassageSelectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResearchSourceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def first_read(self, content, query):
        self.fixture.mirror(content)
        with sqlite3.connect(self.fixture.database) as connection:
            build_index(connection)
        session = self.fixture.knowledge().open_research(character='Example')
        item = session.search(query)['data']['items'][0]
        passage = next(s for s in item['sections'] if s.get('kind') == 'passage')
        return session.read(item['source_id'], passage['section_id'])['data']['text']

    def test_requested_child_beats_parent_assumptions_on_first_read(self):
        text = self.first_read(
            '== Maximum output ==\n'
            'Maximum output assumes standard configuration, usual climate, and ideal calibration. '
            'The configuration assumptions describe the maximum output ceiling.\n'
            '=== Cobalt ===\n'
            'Cobalt reaches seventeen units using four coils.\n'
            '=== Quartz ===\nQuartz reaches eleven units using three coils.',
            'maximum cobalt output configuration assumptions climate calibration ceiling')
        self.assertIn('seventeen units using four coils', text)

    def test_requested_heading_beats_repeated_body_terms_on_first_read(self):
        text = self.first_read(
            '== Formation stages ==\n'
            'Formation has three stages: copper, silver, and gold. '
            'A formation persists for seven minutes between encounters.\n'
            '== Chain strikes ==\n'
            'Formation users can chain strikes with formation training. '
            'The three stages govern chain strikes against each foe. '
            'A strike preserves formation against a foe between attacks. '
            'Formation stages determine strikes against a foe.',
            'three formation stages against foe between attacks')
        self.assertIn('three stages: copper, silver, and gold', text)
        self.assertIn('seven minutes between encounters', text)

    def test_title_only_query_preserves_body_rank_order(self):
        self.first_read(
            '== Synthetic Equipment ==\nSynthetic equipment is a general topic.\n'
            '== Details ==\nSynthetic equipment has a concise rule.',
            'synthetic equipment')
        with sqlite3.connect(self.fixture.database) as connection:
            hits = query_passages(connection, ('synthetic', 'equipment'))
        self.assertGreaterEqual(len(hits), 2)
        self.assertEqual([hit.rank for hit in hits], sorted(hit.rank for hit in hits))

    def test_body_only_match_keeps_best_body_when_headings_do_not_match(self):
        text = self.first_read(
            '== Introduction ==\nCobalt is an ordinary material.\n'
            '== Details ==\nCobalt duration is exactly nineteen beats.\n'
            '== Notes ==\nDuration differs for ordinary materials.',
            'cobalt duration')
        self.assertIn('nineteen beats', text)

    def test_repeated_parent_heading_words_do_not_outweigh_distinct_child(self):
        text = self.first_read(
            '== Cobalt ==\nCobalt has several rules.\n'
            '=== Cobalt Cobalt ===\nCobalt cobalt duration has ordinary settings.\n'
            '=== Duration ===\nThe device runs for thirteen beats.',
            'cobalt duration')
        self.assertIn('thirteen beats', text)


if __name__ == '__main__':
    unittest.main()
