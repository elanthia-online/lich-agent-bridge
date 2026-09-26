"""Synthetic regressions for bounded passage discovery work and source recall."""

import sqlite3
import unittest

from lich_agent_bridge import passage_index as index
from lich_agent_bridge.gswiki import _create_schema, _upsert_page


class PassageQueryCostTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        _create_schema(self.connection)
        self.addCleanup(self.connection.close)

    def page(self, page_id, title, text, *, namespace=0):
        _upsert_page(self.connection, {'pageid': page_id, 'ns': namespace, 'title': title,
            'revisions': [{'revid': 1, 'slots': {'main': {'content': text}}}]}, 'now')

    def test_common_term_does_not_sort_joined_corpus(self):
        for page_id in range(1, 1001):
            self.page(page_id, f'Reference {page_id}', 'Cobalt rules apply to ordinary examples.')
        index.build_index(self.connection)
        operations = 0

        def progress():
            nonlocal operations
            operations += 100
            return 0

        self.connection.set_progress_handler(progress, 100)
        try:
            hits = index.query_passages(self.connection, ('cobalt',), limit=36)
        finally:
            self.connection.set_progress_handler(None, 0)
        self.assertTrue(hits)
        self.assertLessEqual(len(hits), 36)
        # An FTS rank cursor can stop after the bounded matches. Sorting joined
        # source rows first performs work for all 1,000 matching documents.
        self.assertLess(operations, 25000)

    def test_filters_run_before_ranked_lane_limit(self):
        for page_id in range(1, 161):
            self.page(page_id, f'Hidden {page_id}', 'Cobalt.', namespace=12)
            self.page(page_id + 200, f'Former {page_id}', '{{deprecated}} Cobalt.')
        self.page(500, 'Current Rules', 'Cobalt. ' + 'Ordinary details. ' * 30)
        index.build_index(self.connection)
        self.connection.execute('PRAGMA query_only=ON')
        hits = index.query_passages(self.connection, ('cobalt',), limit=3)
        self.assertEqual({hit.page_id for hit in hits}, {500})
        historical = index.query_passages(self.connection, ('cobalt', 'history'), limit=3)
        self.assertTrue(any(200 < hit.page_id < 361 for hit in historical))
        self.assertTrue(all(hit.namespace == 0 for hit in historical))
        other_namespace = index.query_passages(self.connection, ('cobalt',), namespaces=(12,), limit=3)
        self.assertEqual(len(other_namespace), 3)
        self.assertTrue(all(hit.namespace == 12 for hit in other_namespace))

    def test_non_catalog_lane_backfills_beyond_general_limit(self):
        for page_id in range(1, 161):
            self.page(page_id, f'BVShop:Inventory/{page_id}', 'Cobalt.')
        self.page(200, 'Mechanical Rules', 'Cobalt. ' + 'Ordinary details. ' * 30)
        self.page(201, 'Mechanics/saved posts', 'Cobalt. ' + 'Archived details. ' * 40)
        index.build_index(self.connection)
        hits = index.query_passages(self.connection, ('cobalt',), limit=36)
        self.assertTrue({200, 201} <= {hit.page_id for hit in hits})
        self.assertTrue(any(hit.page_id < 161 for hit in hits))
        self.assertEqual(hits, index.query_passages(self.connection, ('cobalt',), limit=36))

    def test_reference_backfill_fills_slots_after_per_source_range_cap(self):
        for page_id in range(1, 13):
            self.page(page_id, f'Rules {page_id}', 'Cobalt.')
        self.page(20, 'BVShop:Inventory', '\n\n'.join(
            f'== Stock {number} ==\nCobalt. Sample.' for number in range(160)))
        for page_id in range(30, 60):
            self.page(page_id, f'Additional Rules {page_id}', 'Cobalt. ' + 'Details. ' * 80)
        index.build_index(self.connection)
        hits = index.query_passages(self.connection, ('cobalt',), limit=36)
        self.assertEqual(len(hits), 36)


if __name__ == '__main__':
    unittest.main()
