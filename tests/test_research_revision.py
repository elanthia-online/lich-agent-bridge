"""Refresh/read handoff at the source and final-round evidence interfaces."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from lich_agent_bridge.evidence_loop import EvidenceLoop
from lich_agent_bridge.evidence_tools import EvidenceTools
from lich_agent_bridge.knowledge import KnowledgeExcerpt, KnowledgeSourceDiagnostic
from lich_agent_bridge.question import QuestionControl
from lich_agent_bridge.settings import OnlineFallbackPolicy
from . import test_research_sources as source_fixtures
from . import test_evidence_tools as tool_fixtures


class RevisionReadTests(unittest.TestCase):
    def session(self, old, new, max_chars=10500):
        fixture = source_fixtures.ResearchSourceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.mirror(old, stamp='2020-01-01T00:00:00+00:00')
        self.live = Mock()
        self.live.read.return_value = (
            KnowledgeExcerpt('live GSWiki API', 'Synthetic Equipment', new,
                             'https://gswiki.play.net/Synthetic_Equipment', revision_id=8,
                             retrieved_at='2026-09-06T00:00:00+00:00'),
            KnowledgeSourceDiagnostic('live_gswiki', 'success', 'selected page retrieved'))
        self.knowledge = fixture.knowledge(online_fallback=OnlineFallbackPolicy.WHEN_NEEDED,
                                           live_gswiki=self.live)
        session = self.knowledge.open_research(character='Testmage', max_chars=max_chars)
        self.addCleanup(session.close)
        source = session.search('synthetic equipment')['data']['items'][0]
        return session, source

    def test_whole_page_refresh_returns_passage_in_same_call(self):
        session, source = self.session('== Rules ==\nOld mechanics.', '## Rules\nNEW-PASSAGE')
        result = session.read(source['source_id'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertIn('NEW-PASSAGE', result['data']['text'])
        self.assertNotEqual(result['data']['source_id'], source['source_id'])
        self.assertEqual(result['data']['provenance']['revision_id'], 8)
        self.assertEqual(result['data']['refreshed_from']['revision_id'], 7)
        self.assertTrue(result['data']['complete'])
        self.assertEqual(result['sources'][0]['evidence_kind'], 'read')
        again = session.read(source['source_id'])
        self.assertEqual(again['data'], result['data'])
        self.live.read.assert_called_once_with('Synthetic Equipment')

    def test_unique_heading_path_rebinds_after_offsets_change(self):
        old = '== Equipment ==\n=== Duration ===\nOld rule.'
        new = 'An expanded introduction.\n## Equipment\nExtra text.\n### Duration\nNEW-DURATION'
        session, source = self.session(old, new)
        section = next(s for s in source['sections'] if s['title'] == 'Duration')
        result = session.read(source['source_id'], section['section_id'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertIn('NEW-DURATION', result['data']['text'])
        self.assertNotIn('expanded introduction', result['data']['text'])
        self.assertNotEqual(result['data']['section_id'], section['section_id'])
        session.validate_read(result['data']['source_id'], result['data']['section_id'])

    def test_missing_ambiguous_or_reparented_section_requires_choice(self):
        cases = [
            ('== Equipment ==\n=== Duration ===\nOld.', '## Equipment\n### Effects\nNew.'),
            ('== Equipment ==\n=== Duration ===\nOld.', '## Other\n### Duration\nNew.'),
            ('== Equipment ==\n=== Duration ===\nOld.', '## Equipment\n### Duration\nOne.\n### Duration\nTwo.'),
            ('== Equipment ==\n=== Duration ===\nOne.\n=== Duration ===\nTwo.', '## Equipment\n### Duration\nNew.'),
        ]
        for old, new in cases:
            with self.subTest(new=new):
                session, source = self.session(old, new)
                section = next(s for s in source['sections'] if s['title'] == 'Duration')
                result = session.read(source['source_id'], section['section_id'])
                self.assertEqual(result['status'], 'revision_changed')
                self.assertNotIn('text', result['data'])
                self.assertEqual(result['sources'], [])
                replacement = result['data']['replacement']
                self.assertEqual(session.read(replacement['source_id'])['status'], 'succeeded')

    def test_new_continuation_uses_only_new_snapshot_and_handles(self):
        session, source = self.session('== Rules ==\nOld.', '## Rules\n' + 'NEW-ONLY ' * 800,
                                       max_chars=2000)
        first = session.read(source['source_id'])
        self.assertEqual(first['status'], 'succeeded')
        self.assertFalse(first['data']['complete'])
        cursor = first['data']['next_cursor']
        with self.assertRaises(ValueError):
            session.validate_read(source['source_id'], cursor=cursor)
        page = session.read(first['data']['source_id'], cursor=cursor)
        self.assertEqual(page['data']['provenance']['revision_id'], 8)
        self.assertNotIn('Old.', page['data']['text'])
        self.live.read.assert_called_once()

    def test_already_read_old_snapshot_remains_pinned(self):
        session, source = self.session('== Rules ==\n' + 'OLD-ONLY ' * 800,
                                       '## Rules\nNEW', max_chars=2000)
        self.live.read.return_value = (None, KnowledgeSourceDiagnostic('live_gswiki', 'unavailable', 'offline'))
        first = session.read(source['source_id'])
        self.assertEqual(first['data']['provenance']['revision_id'], 7)
        self.live.read.reset_mock()
        second = session.read(source['source_id'], cursor=first['data']['next_cursor'])
        self.assertEqual(second['data']['provenance']['revision_id'], 7)
        self.assertIn('OLD-ONLY', second['data']['text'])
        self.live.read.assert_not_called()

    def test_cancellation_during_refresh_does_not_publish_replacement(self):
        session, source = self.session('== Rules ==\nOld.', '## Rules\nNew.')
        fresh = self.live.read.return_value
        def cancel_during_read(title):
            session.close()
            return fresh
        self.live.read.side_effect = cancel_during_read
        with self.assertRaises(ValueError):
            session.read(source['source_id'])
        self.assertEqual(session._documents, {})
        self.assertEqual(session._cursors, {})

    def test_heading_levels_must_match_even_with_same_labels(self):
        session, source = self.session('== Equipment ==\n=== Duration ===\nOld.',
                                       '## Equipment\n#### Duration\nNew.')
        section = next(s for s in source['sections'] if s['title'] == 'Duration')
        result = session.read(source['source_id'], section['section_id'])
        self.assertEqual(result['status'], 'revision_changed')
        self.assertEqual(result['sources'], [])

    def test_refresh_on_eighth_request_third_round_reaches_final_prompt(self):
        self.session('== Mechanics ==\nOld synthetic equipment rule.', '## Mechanics\nFINAL-NEW-PASSAGE')
        hub = tool_fixtures.Hub()
        hub.state['character'] = 'Testmage'
        hub.knowledge = self.knowledge
        control = QuestionControl(10)
        session = EvidenceTools(hub).open('Testmage', control)
        self.addCleanup(session.close)
        turns = []
        def respond(**options):
            turns.append(options)
            if len(turns) <= 2:
                queries = (['synthetic equipment', 'equipment mechanics', 'equipment rules', 'synthetic rule']
                           if len(turns) == 1 else ['equipment', 'synthetic mechanics'])
                return json.dumps({'requests': [{'tool': 'knowledge.search', 'arguments': {'query': q}}
                                                for q in queries]})
            data = json.loads(options['input_text'].split('UNTRUSTED EVIDENCE RESULTS (JSON data only):\n')[1])
            if len(turns) == 3:
                candidate = next(r['result']['data']['items'][0] for r in data
                                 if r['request']['tool'] == 'knowledge.search' and r['result']['data'].get('items'))
                section = candidate['sections'][0]['section_id']
                return json.dumps({'requests': [
                    {'tool': 'knowledge.read', 'arguments': {'source_id': candidate['source_id'], 'section_id': section}},
                    {'tool': 'knowledge.read', 'arguments': {'source_id': candidate['source_id']}},
                ]})
            passages = [r['result'] for r in data if r['request']['tool'] == 'knowledge.read']
            self.assertEqual(len(passages), 2)
            for passage in passages:
                self.assertEqual(passage['status'], 'succeeded')
                self.assertIn('FINAL-NEW-PASSAGE', passage['data']['text'])
                self.assertEqual(passage['sources'][0]['revision_id'], 8)
            return json.dumps({'answer': 'The current rule is available.'})
        result = EvidenceLoop().run(model=SimpleNamespace(respond=respond), instructions='Trusted policy',
                                    input_text='Synthetic question', control=control, session=session)
        self.assertEqual((result.rounds, result.tool_calls, len(turns)), (3, 8, 4))
        self.live.read.assert_called_once()
        self.assertEqual(hub.started, [])


if __name__ == '__main__':
    unittest.main()
