"""Synthetic offline contracts for revision-bound derived wiki passages."""

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lich_agent_bridge.gswiki import _create_schema, _upsert_page
from lich_agent_bridge import passage_index as index
from lich_agent_bridge.wiki_text import research_text


class PassageIndexTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        _create_schema(self.connection)

    def tearDown(self):
        self.connection.close()

    def page(self, page_id, text, *, revision=1, title='Synthetic Rules', namespace=0):
        _upsert_page(self.connection, {'pageid': page_id, 'ns': namespace, 'title': title,
            'revisions': [{'revid': revision, 'slots': {'main': {'content': text}}}]}, 'now')
        self.connection.commit()

    def test_round_trip_and_late_passage(self):
        text = '== Rules ==\n' + ('Early filler facts.\n\n' * 900) + 'Late cobalt combination rule.'
        self.page(1, text)
        index.build_index(self.connection)
        document = index.load_document(self.connection, 1)
        self.assertEqual(document.text, research_text(text))
        hits = index.query_passages(self.connection, ('cobalt',))
        self.assertTrue(hits)
        self.assertGreater(hits[0].start, 10000)
        for hit in hits:
            self.assertEqual(hit.text, document.text[hit.start:hit.text_end])
            self.assertEqual(hit.document_fingerprint,
                hashlib.sha256(document.text.encode()).hexdigest())
            self.assertLessEqual(len(hit.text), index.PASSAGE_CHARS)

    def test_table_retains_caption_headers_and_qualifiers(self):
        text = '== Combinations ==\nOnly ordinary rings qualify.\n\n{| class="wikitable"\n|+ Ring rules\n! Type !! Limit\n|-\n| Cobalt || Two\n|}\n\nExceptions require a special permit.'
        self.page(1, text)
        index.build_index(self.connection)
        hit = index.query_passages(self.connection, ('cobalt',))[0]
        for part in ('Only ordinary rings qualify.', '! Type !! Limit', '| Cobalt || Two',
                     'Exceptions require a special permit.'):
            self.assertIn(part, hit.text)
        self.assertEqual(hit.heading_path, ('Combinations',))

    def test_oversized_table_is_one_flagged_range_without_fabricated_rows(self):
        text = '== Limits ==\nQualifying context.\n\n{|\n! Type !! Limit\n' + '|-\n| cobalt || Two\n' * 300 + '|}\n\nOnly while worn.'
        self.page(1, text)
        index.build_index(self.connection)
        hit = index.query_passages(self.connection, ('cobalt',))[0]
        document = index.load_document(self.connection, 1)
        self.assertTrue(hit.oversized_structure)
        self.assertGreater(hit.end - hit.start, index.PASSAGE_CHARS)
        self.assertLess(hit.text_end, hit.end)
        self.assertEqual(hit.text, document.text[hit.start:hit.text_end])
        self.assertIn('Only while worn.', document.text[hit.start:hit.end])

    def test_unchanged_build_reuses_normalized_text(self):
        self.page(1, 'Cobalt rules.')
        index.build_index(self.connection)
        with patch.object(index, 'research_text', side_effect=AssertionError('renormalized')):
            result = index.build_index(self.connection)
        self.assertEqual(result['reused_documents'], 1)
        self.assertEqual(result['normalized_documents'], 0)

    def test_freshness_only_sync_avoids_both_normalizers(self):
        self.page(1, 'Cobalt rules.')
        index.build_index(self.connection)
        with patch('lich_agent_bridge.gswiki.wikitext_to_text', side_effect=AssertionError('plain renormalized')):
            self.page(1, 'Cobalt rules.')
        with patch.object(index, 'research_text', side_effect=AssertionError('research renormalized')):
            result = index.build_index(self.connection)
        self.assertEqual(result['reused_documents'], 1)

    def test_revision_only_change_rebinds_identical_text(self):
        self.page(1, 'Cobalt rules.')
        index.build_index(self.connection)
        old = index.load_document(self.connection, 1)
        self.page(1, 'Cobalt rules.', revision=2)
        self.assertEqual(index.status(self.connection)['status'], 'invalid')
        index.build_index(self.connection)
        new = index.load_document(self.connection, 1)
        self.assertEqual(old.document_fingerprint, new.document_fingerprint)
        self.assertNotEqual(old.revision_id, new.revision_id)

    def test_selected_document_corruption_fails_closed(self):
        self.page(1, 'Cobalt rules.')
        index.build_index(self.connection)
        self.connection.execute("UPDATE wiki_documents SET text='wrong' WHERE page_id=1")
        with self.assertRaises(index.PassageIndexUnavailable):
            index.load_document(self.connection, 1)

    def test_removed_derived_ranges_fail_closed_and_queries_are_read_only(self):
        self.page(1, 'Cobalt rules.')
        index.build_index(self.connection)
        self.connection.execute('PRAGMA query_only=ON')
        self.assertTrue(index.query_passages(self.connection, ('cobalt',)))
        self.assertIsNotNone(index.load_document(self.connection, 1))
        self.connection.execute('PRAGMA query_only=OFF')
        self.connection.execute('DELETE FROM wiki_passages')
        with self.assertRaises(index.PassageIndexUnavailable):
            index.query_passages(self.connection, ('cobalt',))
        index.build_index(self.connection)
        self.assertTrue(index.query_passages(self.connection, ('cobalt',)))

    def test_heading_like_table_content_does_not_split_table(self):
        self.page(1, '== Rules ==\n{|\n! Type\n|-\n| cobalt\n# literal cell continuation\n|}')
        index.build_index(self.connection)
        hits = index.query_passages(self.connection, ('cobalt',))
        self.assertEqual(hits[0].heading_path, ('Rules',))
        self.assertIn('|}', hits[0].text)

    def test_wal_rebuild_is_rejected_without_replacing_original(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'mirror.sqlite3'
            target = sqlite3.connect(database)
            _create_schema(target)
            target.execute('PRAGMA journal_mode=WAL')
            target.close()
            before = database.read_bytes()
            with self.assertRaisesRegex(index.PassageIndexUnavailable, 'DELETE-journal'):
                index.rebuild(database)
            self.assertEqual(database.read_bytes(), before)

    def test_same_revision_text_change_and_delete_invalidate_ranges(self):
        self.page(1, 'Old cobalt rules.')
        self.page(2, 'Copper rules.')
        index.build_index(self.connection)
        old = index.load_document(self.connection, 1)
        self.page(1, 'New cobalt wording.')
        with self.assertRaises(index.PassageIndexUnavailable):
            index.query_passages(self.connection, ('cobalt',))
        self.connection.execute('DELETE FROM pages WHERE page_id = 2')
        index.build_index(self.connection)
        new = index.load_document(self.connection, 1)
        self.assertNotEqual(old.source_fingerprint, new.source_fingerprint)
        self.assertNotEqual(old.document_fingerprint, new.document_fingerprint)
        self.assertIsNone(index.load_document(self.connection, 2))
        self.assertEqual(index.status(self.connection)['indexed_documents'], 1)

    def test_all_namespaces_stored_but_default_query_policy_unchanged(self):
        self.page(1, 'Cobalt help.', namespace=12)
        index.build_index(self.connection)
        self.assertEqual(index.status(self.connection)['indexed_documents'], 1)
        self.assertEqual(index.query_passages(self.connection, ('cobalt',)), ())
        self.assertEqual(len(index.query_passages(self.connection, ('cobalt',), namespaces=(12,))), 1)

    def test_missing_old_and_corrupt_indexes_fail_without_query_writes(self):
        self.page(1, 'Cobalt rules.')
        for kind in ('missing', 'old', 'corrupt'):
            if kind != 'missing':
                index.build_index(self.connection)
                if kind == 'old':
                    self.connection.execute("UPDATE passage_index_metadata SET value='0' WHERE key='schema_version'")
                else:
                    self.connection.execute('DROP TABLE wiki_passages_fts')
                self.connection.commit()
            before = self.connection.total_changes
            with self.assertRaises(index.PassageIndexUnavailable):
                index.query_passages(self.connection, ('cobalt',))
            self.assertEqual(self.connection.total_changes, before)

    def test_exact_number_redirect_and_non_catalog_lanes(self):
        for number in range(1, 35):
            self.page(number, 'Cobalt ' * 90, title=f'BVShop:Cobalt/{number}')
        self.page(40, 'Cobalt special rule.', title='Cobalt (712)')
        self.page(41, '#REDIRECT [[Cobalt (712)]]', title='Tether')
        self.page(42, '{{deprecated}}\nCobalt former rule.', title='Old rule')
        index.build_index(self.connection)
        self.assertIn(40, {hit.page_id for hit in index.query_passages(self.connection, ('712',))})
        self.assertIn(40, {hit.page_id for hit in index.query_passages(self.connection, ('tether',))})
        hits = index.query_passages(self.connection, ('cobalt',))
        self.assertIn(40, {hit.page_id for hit in hits})
        self.assertNotIn(42, {hit.page_id for hit in hits})

    def test_explicit_rebuild_repairs_corrupt_derived_content(self):
        self.page(1, 'Cobalt baseline rule.')
        index.build_index(self.connection)
        self.connection.execute("UPDATE wiki_passages SET body='Different text' WHERE page_id=1")
        self.connection.commit()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'mirror.sqlite3'
            target = sqlite3.connect(database)
            self.connection.backup(target)
            target.close()
            result = index.rebuild(database)
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(result['normalized_documents'], 1)
            with sqlite3.connect(database) as repaired:
                hits = index.query_passages(repaired, ('cobalt',))
                self.assertIn('Cobalt baseline rule.', ''.join(hit.text for hit in hits))

    def test_atomic_failed_rebuild_preserves_original_bytes(self):
        self.page(1, 'Cobalt rules.')
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'mirror.sqlite3'
            target = sqlite3.connect(database)
            self.connection.backup(target)
            target.close()
            original = database.read_bytes()
            with patch.object(index, 'build_index', side_effect=RuntimeError('synthetic failure')):
                with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                    index.rebuild(database)
            self.assertEqual(database.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [database])
            result = index.rebuild(database)
            self.assertEqual(result['indexed_documents'], 1)


if __name__ == '__main__':
    unittest.main()
