"""Synthetic retrieval regressions through the production research interface."""
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from lich_agent_bridge.gswiki import _create_schema, _upsert_page
from lich_agent_bridge.knowledge import KnowledgeBase


class DiscoveryRankingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.wiki = self.root / 'wiki'
        self.wiki.mkdir()
        self.database = self.root / 'mirror.sqlite3'

    def note(self, filename, title, text):
        (self.wiki / filename).write_text(f'# {title}\n{text}', encoding='utf-8')

    def mirror(self, pages):
        stamp = '2026-09-06T00:00:00+00:00'
        with sqlite3.connect(self.database) as connection:
            _create_schema(connection)
            for index, (title, text) in enumerate(pages, 1):
                _upsert_page(connection, {'pageid': index, 'ns': 0, 'title': title,
                    'revisions': [{'revid': index, 'slots': {'main': {'content': text}}}]}, stamp)
            connection.execute("INSERT OR REPLACE INTO metadata VALUES ('last_sync', ?)", (stamp,))

    def session(self):
        knowledge = KnowledgeBase(wiki_root=self.wiki, gswiki_database=self.database,
                                  now=lambda: datetime(2026, 9, 6, tzinfo=UTC))
        session = knowledge.open_research(character='Example', max_chars=10500)
        self.addCleanup(session.close)
        return session

    def titles(self, query):
        return [item['title'] for item in self.session().search(query)['data']['items']]

    def test_focused_mechanics_survives_keyword_rich_catalogs(self):
        self.note('rules.md', 'Synthetic equipment', '## Weighting\nEquipment weighting follows a fixed rule.')
        for i in range(6):
            self.note(f'shop{i}.md', f'Festival shop {i}',
                      'equipment weighting flares enchant penalties kicks undead price inventory listing\n' * 4)
        titles = self.titles('equipment weighting flares enchant penalties kicks undead')
        self.assertEqual(titles[0], 'Synthetic equipment')

    def test_title_recall_outside_general_fts_shortlist(self):
        pages = [(f'BVShop:Stall {i}/2025', 'synthetic equipment weighting flares enchant ' * 10)
                 for i in range(25)]
        pages.append(('Synthetic equipment', '== Weighting ==\nFixed interaction rule. ' + 'Other details. ' * 800))
        self.mirror(pages)
        self.assertEqual(self.titles('synthetic equipment weighting flares enchant')[0], 'Synthetic equipment')

    def test_non_catalog_body_lane_retains_rule_with_different_title(self):
        pages = [(f'BVShop:Stall {i}/2025', 'synthetic equipment weighting flares enchant ' * 10)
                 for i in range(25)]
        pages.append(('Combat interactions', '== Weighting ==\nSynthetic equipment weighting flares enchant rule. ' + 'Other details. ' * 800))
        self.mirror(pages)
        self.assertIn('Combat interactions', self.titles('synthetic equipment weighting flares enchant'))

    def test_shopping_and_explicit_catalog_queries_keep_catalogs(self):
        self.note('rules.md', 'Equipment weighting', 'Weighting effects and price tradeoffs.')
        self.note('shop.md', 'BVShop:Amber Gallery/2025', 'Buy equipment with weighting. Sale price inventory.')
        self.assertEqual(self.titles('where buy equipment weighting price')[0], 'BVShop:Amber Gallery/2025')
        self.assertEqual(self.titles('Amber Gallery')[0], 'BVShop:Amber Gallery/2025')

    def test_history_query_can_prefer_saved_posts(self):
        self.note('rules.md', 'Equipment weighting', 'Current equipment weighting rules.')
        self.note('history.md', 'Equipment weighting/saved posts', 'Historical equipment weighting before the old rules changed.')
        self.assertEqual(self.titles('equipment weighting history')[0], 'Equipment weighting/saved posts')

    def test_saved_rule_is_not_demoted_below_incidental_title_matches(self):
        pages = [(f'Weighting example {index}', 'A weighting example.') for index in range(6)]
        pages.append(('Mechanism/saved posts',
                      '== Equipment weighting flares ==\nSAVED-RULE: synthetic equipment combines by a fixed rule.'))
        self.mirror(pages)
        session = self.session()
        items = session.search('equipment weighting flares')['data']['items']
        self.assertEqual(items[0]['title'], 'Mechanism/saved posts')
        self.assertEqual(items[0]['ranking']['kind_hint'], 'archive')
        read = session.read(items[0]['source_id'])
        self.assertIn('SAVED-RULE', read['data']['text'])
        self.assertEqual(read['sources'][0]['revision_id'], items[0]['provenance']['revision_id'])

    def test_non_catalog_recall_lane_includes_saved_rules(self):
        pages = [(f'BVShop:Stall {index}/2025', 'equipment weighting flares ' * 10)
                 for index in range(25)]
        pages.append(('Mechanism/saved posts',
                      '== Equipment weighting flares ==\nFixed synthetic rule. ' + 'Other details. ' * 800))
        self.mirror(pages)
        self.assertIn('Mechanism/saved posts', self.titles('equipment weighting flares'))

    def test_distinct_archived_sections_are_not_treated_as_catalog_editions(self):
        targets = ['Equipment weighting/saved posts', 'Equipment weighting/archive']
        pages = [(title, '== Equipment weighting flares ==\nA distinct synthetic rule.') for title in targets]
        pages.extend((f'Weighting example {index}', 'A weighting example.') for index in range(6))
        self.mirror(pages)
        titles = self.titles('equipment weighting flares')
        self.assertTrue(set(targets) <= set(titles), titles)

    def test_catalog_editions_do_not_fill_slots_ahead_of_independent_source(self):
        for year in range(2018, 2026):
            self.note(f'shop{year}.md', f'BVShop:Amber Gallery/February {year}', 'Buy equipment weighting price.')
        self.note('alternative.md', 'CIShop:Silver Gallery/2025', 'Buy equipment weighting price.')
        titles = self.titles('buy equipment weighting price')
        self.assertIn('CIShop:Silver Gallery/2025', titles[:2])
        self.assertEqual(self.titles('Amber Gallery 2022')[0], 'BVShop:Amber Gallery/February 2022')

    def test_substring_only_match_does_not_qualify_as_term(self):
        self.note('false.md', 'Scare display', 'scare scare scare')
        self.assertEqual(self.titles('scar'), [])

    def test_equal_scores_have_stable_source_order(self):
        self.note('z.md', 'Zeta rules', 'Synthetic equipment.')
        self.note('a.md', 'Alpha rules', 'Synthetic equipment.')
        self.assertEqual(self.titles('synthetic equipment'), ['Alpha rules', 'Zeta rules'])

    def test_match_explanations_do_not_replace_source_provenance(self):
        self.note('rules.md', 'Synthetic equipment', '## Weighting\nFixed rule.')
        session = self.session()
        result = session.search('synthetic equipment weighting')
        item = result['data']['items'][0]
        self.assertIn('ranking', item)
        self.assertEqual(item['evidence_kind'], 'discovery')
        read = session.read(item['source_id'])
        self.assertIn('Fixed rule.', read['data']['text'])
        self.assertEqual(read['data']['provenance']['revision_id'], item['provenance']['revision_id'])
        self.assertLessEqual(len(json.dumps(result)), 12000)

    def test_numbered_spell_title_outranks_incidental_number(self):
        self.mirror([
            ('Alchemy 1234', 'A numbered recipe mentioning 1234 and a spell.'),
            ('Example Ward (1234)', '== Duration ==\nThis spell lasts seventeen beats.'),
        ])
        self.assertEqual(self.titles('1234 spell')[0], 'Example Ward (1234)')

    def test_repetition_does_not_change_ranking(self):
        self.note('a.md', 'Alpha', 'equipment weighting')
        self.note('z.md', 'Zeta', 'equipment weighting')
        before = self.titles('equipment weighting')
        self.note('z.md', 'Zeta', 'equipment weighting ' * 1000)
        self.assertEqual(self.titles('equipment weighting'), before)

    def test_catalog_only_results_use_deferred_editions_as_backfill(self):
        for year in range(2020, 2026):
            self.note(f'{year}.md', f'BVShop:Amber Gallery/{year}', 'Buy equipment.')
        self.assertEqual(len(self.titles('buy equipment')), 6)


if __name__ == '__main__':
    unittest.main()
