"""Offline evidence-to-prompt cases; synthetic mechanics are test data only."""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

from lich_agent_bridge.context_assembler import ContextAssembler
from lich_agent_bridge.gswiki import sync
from lich_agent_bridge.knowledge import KnowledgeBase, KnowledgeExcerpt, KnowledgeSourceDiagnostic, LiveGSWikiSource
from lich_agent_bridge.settings import OnlineFallbackPolicy

from .test_knowledge import FakeResponse, LiveOpener


class KnowledgeQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wiki = self.root / "wiki"
        notes = {
            "characters/Testmage.md": "# Testmage\nVerified sorcerer with a staff.\n",
            "characters/Testmage-Spells.md": "# Spell choices\nSynthetic training example: Testmage practices 711 once at a target dummy.\n",
            "characters/Testscout-Spells.md": "# Spell choices\nTestscout private-warrior-plan uses 711.\n",
            "characters/README.md": "# All characters\nTestscout private-index-fact uses 711.\n",
            "project/Current-State.md": "# Current mechanics\n706 and 716 updated mechanics are covered by our implementation plans.\n",
            "project/Architecture.md": "# LAB developer API\nRegistered operations use snapshot, capabilities, perform and watch for automated tests.\n",
            "gsiv/spells.md": "# Spell overview\n706 and 716 updated spell mechanics should be checked.\n",
        }
        for relative, text in notes.items():
            target = self.wiki / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.database = self.root / "mirror.sqlite3"
        pages = [
            (706, "Tenebrous Tether (706)", "Tether fixture: roots and pulses damage."),
            (716, "Pestilence (716)", "Pestilence fixture: defensive and offensive uses."),
            (1000, "Buff spells", "706 and 716 updated spell mechanics appear in this index."),
            (7062, "Tenebrous Tether (706)/Mind Jolt (706)", "{{deprecated | spell}} Historical fixture: Mind Jolt stuns for a warding margin."),
            (7063, "Alchemy 706", "#REDIRECT [[missing lowercase target]]"),
        ]

        def opener(_request, **_kwargs):
            return FakeResponse({"batchcomplete": True, "query": {"pages": [
                {"pageid": number, "ns": 0, "title": title, "revisions": [{
                    "revid": number, "timestamp": "2026-09-06T00:00:00Z",
                    "slots": {"main": {"content": text}},
                }]} for number, title, text in pages
            ]}})

        sync(self.database, namespaces=(0,), delay=0, opener=opener)

    def knowledge(self, **options):
        return KnowledgeBase(wiki_root=self.wiki, gswiki_database=self.database, **options)

    def test_focused_gameplay_followups_do_not_spend_slots_on_incidental_gear_mentions(self):
        # These are invented passages for ranking tests, not game mechanics.
        notes = {
            "Unarmed.md": "# Unarmed attacks\nSynthetic combat fixture: holding a weapon changes unarmed attacks. Held equipment has its own combat rules.\n",
            "Locksmith.md": "# Locksmith service\nSynthetic locksmith-only fixture: the player stores a weapon before submitting a locked box. A weapon is not a box.\n",
            "Resurrection.md": "# Resurrection guide\nSynthetic resurrection-only fixture: a player stows a weapon before handling a body.\n",
            "Conditions.md": "# Equipment conditions\nSynthetic supporting fixture: weapon restrictions also affect unarmed combat.\n",
        }
        for filename, text in notes.items():
            (self.wiki / "gsiv" / filename).write_text(text)
        for query in (
            "How does holding a weapon affect unarmed attacks?",
            "Previous player question: How do unarmed attacks work?\nClarification: What changes when I hold a weapon?",
        ):
            with self.subTest(query=query):
                knowledge = self.knowledge()
                selected = knowledge.search(character="Testmage", question=query)
                self.assertTrue(any("Synthetic combat fixture" in item.text for item in selected))
                self.assertTrue(any("Synthetic supporting fixture" in item.text for item in selected))
                self.assertFalse(any("locksmith-only" in item.text or "resurrection-only" in item.text for item in selected))
                prompt = ContextAssembler(knowledge=knowledge).build(
                    character="Testmage", question=query,
                ).to_prompt()
                self.assertIn("Synthetic combat fixture", prompt)
                self.assertNotIn("locksmith-only", prompt)
                self.assertNotIn("resurrection-only", prompt)

    def test_curated_body_only_single_term_and_focused_title_queries_remain_available(self):
        (self.wiki / "gsiv" / "General.md").write_text(
            "# Equipment\nSynthetic noun fixture: this badge is decorative.\n"
        )
        (self.wiki / "gsiv" / "Locksmith.md").write_text(
            "# Locksmith service\nSynthetic service fixture: submit a locked box.\n"
        )
        cases = [(query, "Synthetic noun fixture") for query in (
            "badge", "Tell me about badge", "Explain badge", "Describe badge", "Show me badge",
        )]
        cases.append(("What does the locksmith charge?", "Synthetic service fixture"))
        for query, expected in cases:
            with self.subTest(query=query):
                selected = self.knowledge().search(character="Testmage", question=query)
                self.assertTrue(any(expected in item.text for item in selected))
                prompt = ContextAssembler(knowledge=self.knowledge()).build(
                    character="Testmage", question=query,
                ).to_prompt()
                self.assertIn(expected, prompt)

    def test_development_query_keeps_single_body_match_for_named_interface(self):
        (self.wiki / "project" / "Adapter.md").write_text(
            "# Integration boundary\nSynthetic developer fixture: WidgetAPI exposes observations.\n"
        )
        selected = self.knowledge().search(character="Testmage", question="How do I call WidgetAPI from my script?")
        self.assertTrue(any("Synthetic developer fixture" in item.text for item in selected))

    def test_gameplay_spell_questions_select_mechanics_before_incidental_notes(self):
        knowledge = self.knowledge()
        cases = (
            ("What are the updated mechanics of 706?", "Tenebrous Tether (706)", "roots and pulses"),
            ("What does Tenebrous Tether do?", "Tenebrous Tether (706)", "roots and pulses"),
            ("Can I use Pestilence 716 offensively?", "Pestilence (716)", "defensive and offensive"),
        )
        for question, expected_title, expected_text in cases:
            with self.subTest(question=question):
                selected = knowledge.search(character="Testmage", question=question)
                self.assertEqual(selected[0].title, expected_title)
                prompt = ContextAssembler(knowledge=knowledge).build(
                    character="Testmage", question=question
                ).to_prompt()
                self.assertIn(expected_text, prompt)
                self.assertNotIn("implementation plans", prompt)
                self.assertNotIn("wiki/project/", prompt)

    def test_current_spell_followup_excludes_deprecated_and_unresolved_redirect_pages(self):
        query = "Previous player question: What does Tenebrous Tether 706 do?\nClarification: How long does it last?"
        current = self.knowledge().search(character="Testmage", question=query)
        self.assertFalse(any("/Mind Jolt" in item.title for item in current))
        self.assertFalse(any(item.title == "Alchemy 706" for item in current))
        historical = self.knowledge().search(character="Testmage", question="How did old Mind Jolt 706 work?")
        self.assertTrue(any("/Mind Jolt" in item.title for item in historical))

    def test_historical_live_lookup_does_not_contaminate_current_spell_cache(self):
        opener = LiveOpener({"query": {"pages": [
            {"title": title, "revisions": [{"revid": number, "slots": {"main": {"content": text}}}]}
            for number, title, text in (
                (1, "Tether (706)", "Current fixture roots."),
                (2, "Tether (706)/Old spell", "{{deprecated | spell}} Historical fixture stuns."),
            )
        ]}})
        source = LiveGSWikiSource(cache_path=self.root / "live.json", opener=opener, min_request_interval_seconds=0)
        historical, _ = source.search("old 706", ("old", "706"))
        current, _ = source.search("706 duration", ("706", "duration"))
        self.assertTrue(any("Historical fixture" in item.text for item in historical))
        self.assertFalse(any("Historical fixture" in item.text for item in current))
        self.assertEqual(len(opener.calls), 2)

    def test_developer_questions_keep_the_operation_contract_in_the_prompt(self):
        prompt = ContextAssembler(knowledge=self.knowledge()).build(
            character="Testmage", question="How does the LAB developer API run automated tests?"
        ).to_prompt()
        self.assertIn("snapshot, capabilities, perform and watch", prompt)
        self.assertIn("wiki/project/Architecture.md", prompt)

    def test_character_topic_pages_are_available_without_another_characters_notes(self):
        prompt = ContextAssembler(knowledge=self.knowledge()).build(
            character="Testmage", question="What is my 711 practice sequence for the target dummy?"
        ).to_prompt()
        self.assertIn("711 once at a target dummy", prompt)
        self.assertIn("Testmage-Spells.md", prompt)
        self.assertNotIn("private-warrior-plan", prompt)
        self.assertNotIn("private-index-fact", prompt)

    def test_personal_skill_followup_keeps_recorded_ranks_and_observation_date(self):
        (self.wiki / "characters" / "Testmage.md").write_text(
            "# Testmage\n\nLast updated: 2026-09-04\n\n"
            "## Verified\nMagical skills: 82 ranks each in Elemental Mana Control and "
            "Spiritual Mana Control; 73 Sorcerous Lore (Necromancy).\n\n"
            "## Player-reported\n" + ("Tenebrous Tether 706 is used in my rotation. " * 25)
        )
        (self.wiki / "characters" / "Testmage-Spells.md").write_text(
            "# Offensive spells\n" + ("Tenebrous Tether 706 roots targets. " * 25)
        )
        for followup in ("How long does it last with my skills?", "With my training?", "How does my lore affect it?"):
            with self.subTest(followup=followup):
                prior = "What does Tenebrous Tether 706 do?"
                assembled = ContextAssembler(knowledge=self.knowledge()).build(
                    character="Testmage", question=followup,
                    knowledge_question=f"Previous player question: {prior}\nClarification: {followup}",
                    follow_up_question=prior,
                )
                prompt, sources = assembled.render()
                self.assertIn("82 ranks each", prompt)
                self.assertIn("73 Sorcerous Lore", prompt)
                self.assertIn("Last updated: 2026-09-04", prompt)
                self.assertIn("Historical character record; not a live skills observation", prompt)
                self.assertTrue(any(item["source"] == "wiki/characters/Testmage.md" and item["title"] == "Verified" for item in sources))
                self.assertIn("Tether fixture: roots and pulses damage", prompt)

    def test_sufficient_local_evidence_skips_both_network_providers(self):
        for policy in (OnlineFallbackPolicy.WHEN_NEEDED, OnlineFallbackPolicy.DISABLED):
            with self.subTest(policy=policy):
                live, web = Mock(), Mock()
                web.search.return_value = ([], KnowledgeSourceDiagnostic("general_web", "empty", "no match"))
                selected = self.knowledge(
                    online_fallback=policy, live_gswiki=live, general_web=web
                ).search(character="Testmage", question="What are the mechanics of 706?")
                live.search.assert_not_called()
                web.search.assert_not_called()
                self.assertEqual(selected.diagnostics[3].status, "not_needed")

    def test_stale_duplicate_is_replaced_before_a_single_excerpt_budget(self):
        live, web = Mock(), Mock()
        live.search.return_value = ([KnowledgeExcerpt(
            authority="live GSWiki API", title="Tenebrous Tether (706)",
            text="Fresh tether fixture: current roots and pulses.",
            source="https://gswiki.play.net/Tenebrous_Tether_%28706%29#Mechanics",
            revision_id=9000, retrieved_at="2027-01-01T00:00:00Z",
        )], KnowledgeSourceDiagnostic("live_gswiki", "success", "matched 1 excerpt"))
        selected = self.knowledge(
            now=lambda: datetime(2027, 1, 1, tzinfo=UTC),
            max_excerpts=1, online_fallback=OnlineFallbackPolicy.WHEN_NEEDED,
            live_gswiki=live, general_web=web,
        ).search(character="Testmage", question="What are the updated mechanics of 706?")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].revision_id, 9000)
        self.assertIn("Fresh tether fixture", selected[0].text)
        self.assertEqual(selected.diagnostics[1].status, "stale")
        web.search.assert_not_called()
        full = self.knowledge(
            now=lambda: datetime(2027, 1, 1, tzinfo=UTC),
            online_fallback=OnlineFallbackPolicy.WHEN_NEEDED,
            live_gswiki=live, general_web=web,
        ).search(character="Testmage", question="706 mechanics")
        self.assertEqual(
            [item.revision_id for item in full if item.title == "Tenebrous Tether (706)"],
            [9000],
        )

    def test_failed_fresh_lookup_preserves_stale_evidence_and_diagnostic(self):
        live = Mock()
        live.search.return_value = ([], KnowledgeSourceDiagnostic("live_gswiki", "unavailable", "offline"))
        selected = self.knowledge(
            now=lambda: datetime(2027, 1, 1, tzinfo=UTC),
            online_fallback=OnlineFallbackPolicy.WHEN_NEEDED, live_gswiki=live,
        ).search(character="Testmage", question="706 mechanics")
        self.assertEqual(selected[0].revision_id, 706)
        self.assertEqual(selected.diagnostics[1].status, "stale")
        self.assertEqual(selected.diagnostics[2].status, "unavailable")

    def test_spell_comparison_requires_evidence_for_both_numbers(self):
        live = Mock()
        live.search.return_value = ([], KnowledgeSourceDiagnostic("live_gswiki", "empty", "no match"))
        self.knowledge(
            online_fallback=OnlineFallbackPolicy.WHEN_NEEDED, live_gswiki=live,
        ).search(character="Testmage", question="Compare 706 and 999 mechanics")
        live.search.assert_called_once()
