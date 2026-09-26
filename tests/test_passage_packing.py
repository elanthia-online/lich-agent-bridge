"""Budget regressions at the public research search/read interface."""
import json
import sqlite3
import unittest

from lich_agent_bridge.gswiki import _upsert_page
from lich_agent_bridge.passage_index import build_index
from . import test_research_sources as fixtures


class PassagePackingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResearchSourceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def many_outlines(self, count=3, long_preview=False):
        description = ' Ordinary descriptive background.' * 40 if long_preview else ''
        self.fixture.mirror('== Cobalt weighting ==\nCobalt weighting rule one.' + description + '\n'
                            + ''.join(f'== Appendix {i} ==\nOrdinary material.\n' for i in range(20)))
        with sqlite3.connect(self.fixture.database) as connection:
            titles = ['Cobalt rules', 'Weighting rules'] + [f'Cobalt reference {i}' for i in range(4, count + 1)]
            for page_id, title in enumerate(titles, start=2):
                _upsert_page(connection, {'pageid': page_id, 'ns': 0, 'title': title,
                    'revisions': [{'revid': page_id, 'slots': {'main': {'content':
                        f'== Cobalt weighting ==\nCobalt weighting rule {page_id}.' + description + '\n'
                        + ''.join(f'== Appendix {i} ==\nOrdinary material.\n' for i in range(20))}}}]},
                    '2026-09-06T00:00:00+00:00')
            build_index(connection)

    def test_long_outlines_preserve_other_sources_and_readable_matches(self):
        self.many_outlines()
        session = self.fixture.knowledge().open_research(character='Example', max_chars=6000)
        result = session.search('cobalt weighting')
        items = result['data']['items']
        self.assertEqual({item['title'] for item in items},
                         {'Synthetic Equipment', 'Cobalt rules', 'Weighting rules'})
        self.assertLessEqual(len(json.dumps({'items': items, 'sources': result['sources']})), 6000)
        for item in items:
            with self.subTest(title=item['title']):
                passage = next(section for section in item['sections'] if section['kind'] == 'passage')
                section = next(section for section in item['sections'] if section['kind'] == 'section')
                read = session.read(item['source_id'], passage['section_id'])
                self.assertIn('Cobalt weighting rule', read['data']['text'])
                self.assertEqual(read['sources'][0]['revision_id'], item['provenance']['revision_id'])
                self.assertIn('Cobalt weighting rule', session.read(item['source_id'], section['section_id'])['data']['text'])
                self.assertIn('Appendix', session.read(item['source_id'])['data']['text'])
                self.assertFalse(item['outline_complete'])

    def test_duplicate_long_previews_do_not_displace_a_readable_source(self):
        self.many_outlines(count=4, long_preview=True)
        session = self.fixture.knowledge().open_research(character='Example', max_chars=6000)
        result = session.search('cobalt weighting')
        items = result['data']['items']
        self.assertEqual(len(items), 4)
        self.assertLessEqual(len(json.dumps({'items': items, 'sources': result['sources']})), 6000)
        for item in items:
            self.assertIn('Cobalt weighting', item['snippet'])
            self.assertTrue(item['snippet'].endswith('…'))
            first = item['sections'][0]
            self.assertEqual(first['kind'], 'passage')
            self.assertNotIn('snippet', first)
            read = session.read(item['source_id'], first['section_id'])
            self.assertIn('Cobalt weighting rule', read['data']['text'])

    def test_six_sources_keep_matches_at_default_allowance(self):
        self.many_outlines(count=6)
        session = self.fixture.knowledge().open_research(character='Example', max_chars=10500)
        result = session.search('cobalt weighting')
        items = result['data']['items']
        self.assertEqual(len(items), 6)
        self.assertLessEqual(len(json.dumps({'items': items, 'sources': result['sources']})), 10500)
        for item in items:
            passage = next(section for section in item['sections'] if section['kind'] == 'passage')
            self.assertIn('Cobalt weighting rule', session.read(item['source_id'], passage['section_id'])['data']['text'])

    def test_tight_allowance_reports_omissions_without_bare_sources(self):
        self.many_outlines()
        for allowance in (256, 2000):
            with self.subTest(allowance=allowance):
                session = self.fixture.knowledge().open_research(character='Example', max_chars=allowance)
                result = session.search('cobalt weighting')
                items = result['data']['items']
                self.assertEqual(result['status'], 'partial')
                self.assertTrue(any('candidates omitted' in diagnostic.get('detail', '')
                                    for diagnostic in result['diagnostics']))
                self.assertLessEqual(len(json.dumps({'items': items, 'sources': result['sources']})), allowance)
                self.assertEqual(len(items), 0 if allowance == 256 else 1)
                for item in items:
                    self.assertEqual(item['sections'][0]['kind'], 'passage')
                    self.assertFalse(item['outline_complete'])
                    self.assertIn('Cobalt weighting rule',
                                  session.read(item['source_id'], item['sections'][0]['section_id'])['data']['text'])


if __name__ == '__main__':
    unittest.main()
