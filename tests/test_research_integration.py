"""Synthetic search/read-to-answer cases; no server, model API, or game I/O."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from lich_agent_bridge.context_assembler import ContextAssembler
from lich_agent_bridge.engine import Copilot
from lich_agent_bridge.errors import ModelError, QuestionInvalidated, ValidationError
from lich_agent_bridge.evidence_tools import EvidenceTools
from lich_agent_bridge.knowledge import KnowledgeBase
from lich_agent_bridge.protocol import AskRequest
from lich_agent_bridge.question import QuestionControl

from .test_evidence_tools import Hub
from .test_evidence_integration import Turns, request


def records(prompt):
    return json.loads(prompt.split("UNTRUSTED EVIDENCE RESULTS (JSON data only):\n", 1)[1])


class ResearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "gsiv").mkdir()
        (self.root / "gsiv" / "Mechanisms.md").write_text(
            "# Synthetic mechanisms\n## Introduction\n"
            + "An unrelated introductory observation. " * 900
            + "\n## Late rule\nLATE-RULE-MARKER: the fictional mechanism lasts exactly 17 beats.\n",
            encoding="utf-8",
        )
        self.knowledge = KnowledgeBase(wiki_root=self.root)
        self.hub = Hub()
        self.hub.knowledge = self.knowledge

    def test_deliberate_late_section_read_reaches_answer_with_read_provenance(self):
        calls = []
        issued = {}
        selected_range = []

        def respond(**options):
            calls.append(options)
            prompt = options["input_text"]
            if len(calls) == 1:
                self.assertNotIn("An unrelated introductory observation", prompt)
                self.assertNotIn("LATE-RULE-MARKER", prompt)
                return json.dumps(request("knowledge.search", query="synthetic mechanisms"))
            if len(calls) == 2:
                candidates = next(record["result"]["data"]["items"] for record in records(prompt)
                                  if record.get("request", {}).get("tool") == "knowledge.search")
                candidate = next(item for item in candidates if "Synthetic mechanisms" in item["title"])
                section = next(item for item in candidate["sections"] if item["title"] == "Late rule")
                selected_range[:] = [section["start"], section["end"]]
                self.assertIn("half-open character ranges", options["instructions"])
                self.assertIn("parent range includes its subsections", options["instructions"])
                issued.update(source_id=candidate["source_id"], section_id=section["section_id"])
                return json.dumps(request("knowledge.read", **issued))
            self.assertIn("LATE-RULE-MARKER", prompt)
            passage = next(record["result"]["data"] for record in records(prompt)
                           if record.get("request", {}).get("tool") == "knowledge.read")
            self.assertEqual([passage["start"], passage["end"]], selected_range)
            self.assertNotIn("An unrelated introductory observation. " * 100, prompt)
            return json.dumps({"answer": "The fictional mechanism lasts 17 beats."})

        model = SimpleNamespace(backend="synthetic", configured=True, respond=respond,
                                timing_metadata={})
        opened = []
        original_open = self.knowledge.open_research

        def open_research(**options):
            session = original_open(**options)
            opened.append(session)
            return session

        self.knowledge.open_research = open_research
        copilot = Copilot(model, context_assembler=ContextAssembler(knowledge=self.knowledge),
                          evidence_tools=EvidenceTools(self.hub))
        answer = copilot.ask(AskRequest("Testmage", "Explain the synthetic mechanism's duration."))
        self.assertEqual(len(calls), 3)
        self.assertIn("17 beats", answer.text)
        self.assertTrue(any(source.get("evidence_kind") == "read" for source in answer.sources))
        self.assertEqual(self.hub.started, [])
        with self.assertRaises(ValueError):
            opened[0].validate_read(**issued)

    def test_initial_research_context_skips_search_but_legacy_assembly_keeps_it(self):
        knowledge = Mock()
        knowledge.search.return_value = ()
        assembler = ContextAssembler(knowledge=knowledge)
        packet = assembler.build(character="Testmage", question="Mechanics?", include_knowledge=False)
        self.assertEqual(packet.knowledge_excerpts, ())
        knowledge.search.assert_not_called()
        assembler.build(character="Testmage", question="Mechanics?")
        knowledge.search.assert_called_once()

    def test_ranked_rule_survives_catalog_noise_and_reaches_final_prompt(self):
        (self.root / 'gsiv' / 'equipment.md').write_text(
            '# Synthetic equipment\n## Weighting\nRANKED-RULE-MARKER: equipment weighting follows a fixed rule.')
        for i in range(8):
            (self.root / 'gsiv' / f'shop{i}.md').write_text(
                f'# BVShop:Stall {i}/2025\n' +
                'equipment weighting flares enchant penalties kicks undead price\n' * 4)
        prompts = []
        def respond(**options):
            prompt = options['input_text']
            prompts.append(prompt)
            if len(prompts) == 1:
                return json.dumps(request('knowledge.search',
                    query='equipment weighting flares enchant penalties kicks undead'))
            if len(prompts) == 2:
                item = records(prompt)[0]['result']['data']['items'][0]
                self.assertEqual(item['title'], 'Synthetic equipment')
                section = next(s for s in item['sections'] if s['title'] == 'Weighting')
                return json.dumps(request('knowledge.read', source_id=item['source_id'],
                                          section_id=section['section_id']))
            read = next(r['result'] for r in records(prompt) if r['request']['tool'] == 'knowledge.read')
            self.assertIn('RANKED-RULE-MARKER', read['data']['text'])
            self.assertTrue(read['data']['complete'])
            return json.dumps({'answer': 'The fixed rule was read.'})
        model = SimpleNamespace(backend='synthetic', configured=True, respond=respond, timing_metadata={})
        answer = Copilot(model, context_assembler=ContextAssembler(knowledge=self.knowledge),
                         evidence_tools=EvidenceTools(self.hub)).ask(AskRequest('Testmage', 'Equipment interactions?'))
        self.assertTrue(any(s.get('evidence_kind') == 'read' for s in answer.sources))
        self.assertEqual(len(prompts), 3)
        self.assertEqual(self.hub.started, [])

    def test_invalid_read_rejects_whole_batch_before_recon(self):
        model = Turns({"requests": [
            {"tool": "character.read", "arguments": {}},
            {"tool": "knowledge.read", "arguments": {"source_id": "src_0000000000000000"}},
        ]})
        copilot = Copilot(model, evidence_tools=EvidenceTools(self.hub))
        with self.assertRaises(ModelError):
            copilot.ask(AskRequest("Testmage", "Check these observations."))
        self.assertEqual(self.hub.started, [])

    def test_handles_are_question_local_and_scope_arguments_are_strict(self):
        control = QuestionControl(10)
        session = EvidenceTools(self.hub).open("Testmage", control)
        other = EvidenceTools(self.hub).open("Testmage", control)
        self.addCleanup(session.close)
        self.addCleanup(other.close)
        found = session.execute("knowledge.search", {"query": "synthetic mechanisms"}, control)
        handle = found["data"]["items"][0]["source_id"]
        session.validate("knowledge.read", {"source_id": handle})
        with self.assertRaises(ValidationError):
            other.validate("knowledge.read", {"source_id": handle})
        for args in ({"source_id": "/etc/passwd"}, {"source_id": "https://example.org"},
                     {"source_id": handle, "cursor": None}, {"source_id": handle, "character": "Testscout"}):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                session.validate("knowledge.read", args)
        with self.assertRaises(ValidationError):
            session.validate("knowledge.search", {"query": "mechanisms", "scope": "all"})
        self.hub.state["generation"] = "replacement"
        with self.assertRaises(QuestionInvalidated):
            session.execute("knowledge.read", {"source_id": handle}, control)

    def test_cancel_synchronously_invalidates_research_handles(self):
        control = QuestionControl(10)
        session = EvidenceTools(self.hub).open("Testmage", control)
        self.addCleanup(session.close)
        result = session.execute("knowledge.search", {"query": "synthetic mechanisms"}, control)
        source_id = result["data"]["items"][0]["source_id"]
        control.cancel("forgotten")
        with self.assertRaises(ValidationError):
            session.validate("knowledge.read", {"source_id": source_id})
        self.assertEqual(self.hub.started, [])


if __name__ == "__main__":
    unittest.main()
