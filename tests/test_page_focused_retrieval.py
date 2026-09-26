"""Selected-page recovery from immutable indexed ranges, not global recall."""

import hashlib
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import patch

from lich_agent_bridge.passage_index import PassageIndexUnavailable, build_index, query_passages
from . import test_research_sources as fixtures


class PageFocusedRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResearchSourceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def selected(self, content, terms=('synthetic', 'equipment')):
        self.fixture.mirror(content)
        with sqlite3.connect(self.fixture.database) as connection:
            build_index(connection)
            hits = query_passages(connection, terms)
        knowledge = self.fixture.knowledge()
        excerpt = knowledge._load_research_document(hits[0], retrieved_at='synthetic')
        return knowledge, hits[0], excerpt, hits

    def test_recovers_fact_after_global_per_source_cap(self):
        content = ''.join(f'== Cobalt {i} ==\nCobalt cobalt cobalt.\n' for i in range(12))
        content += '== Details ==\n' + 'Ordinary material. ' * 30
        content += 'Cobalt and quartz keep twelve charges after rinsing.'
        knowledge, hit, excerpt, initial = self.selected(content, ('cobalt',))
        self.assertEqual(len(initial), 3)
        self.assertFalse(any('twelve charges' in row.text for row in initial))
        recovered = knowledge._research_document_passages(
            hit, excerpt, terms=('quartz', 'charges', 'rinsing'))
        self.assertIn('twelve charges', recovered[0].text)
        self.assertLessEqual(len(recovered), 3)
        self.assertEqual(excerpt.text[recovered[0].start:recovered[0].text_end], recovered[0].text)

    def test_uses_query_tail_without_another_document_load_or_normalization(self):
        knowledge, hit, excerpt, _ = self.selected(
            '== Overview ==\nSynthetic equipment has cobalt facts.\n'
            '== Details ==\nQuartz resonance ends after eleven beats.')
        terms = ('ordinary', 'general', 'topic', 'material', 'item', 'rule', 'use',
                 'guide', 'other', 'details', 'quartz', 'resonance')
        with patch.object(knowledge, '_load_research_document', side_effect=AssertionError('duplicate load')), \
             patch('lich_agent_bridge.wiki_text.research_text', side_effect=AssertionError('reparse')):
            recovered = knowledge._research_document_passages(hit, excerpt, terms=terms)
        self.assertIn('eleven beats', recovered[0].text)

    def test_rejects_rebound_or_modified_snapshot_without_writing(self):
        knowledge, hit, excerpt, _ = self.selected('== Details ==\nQuartz retains twelve charges.')
        before = hashlib.sha256(self.fixture.database.read_bytes()).hexdigest()
        for changed in (replace(hit, revision_id=999), replace(hit, document_fingerprint='wrong')):
            with self.subTest(changed=changed.revision_id), self.assertRaises(PassageIndexUnavailable):
                knowledge._research_document_passages(changed, excerpt, terms=('quartz',))
        with self.assertRaises(PassageIndexUnavailable):
            knowledge._research_document_passages(hit, replace(excerpt, text='different'), terms=('quartz',))
        self.assertEqual(hashlib.sha256(self.fixture.database.read_bytes()).hexdigest(), before)

    def test_never_returns_unmatched_ranges_for_unrelated_query(self):
        knowledge, hit, excerpt, _ = self.selected('== Details ==\nQuartz retains twelve charges.')
        self.assertEqual(knowledge._research_document_passages(hit, excerpt, terms=('unrelated',)), ())

    def test_explicit_page_bounds_reject_partial_recovery(self):
        knowledge, hit, excerpt, _ = self.selected(
            '== One ==\nQuartz one.\n== Two ==\nQuartz two.\n== Three ==\nQuartz three.')
        with patch('lich_agent_bridge.passage_index._FOCUSED_PASSAGE_ROWS', 2):
            with self.assertRaisesRegex(PassageIndexUnavailable, 'passage allowance'):
                knowledge._research_document_passages(hit, excerpt, terms=('quartz',))
        with patch('lich_agent_bridge.passage_index._FOCUSED_DOCUMENT_CHARS', 1):
            with self.assertRaisesRegex(PassageIndexUnavailable, 'document allowance'):
                knowledge._research_document_passages(hit, excerpt, terms=('quartz',))

    def test_selected_page_recovery_does_not_read_other_page_ranges(self):
        knowledge, hit, excerpt, _ = self.selected('== Details ==\nQuartz retains twelve charges.')
        statements = []
        from lich_agent_bridge.passage_index import document_passages
        with sqlite3.connect(f'file:{self.fixture.database}?mode=ro', uri=True) as connection:
            connection.set_trace_callback(statements.append)
            connection.execute('BEGIN')
            recovered = document_passages(connection, hit, excerpt.text, ('quartz',))
        self.assertTrue(recovered)
        range_reads = [sql for sql in statements if 'FROM wiki_passages WHERE' in sql]
        self.assertEqual(len(range_reads), 1)
        self.assertIn('WHERE page_id=1', range_reads[0])
        self.assertIn('LIMIT 4097', range_reads[0])
        self.assertFalse(any('MATCH' in sql for sql in statements))


if __name__ == '__main__':
    unittest.main()
