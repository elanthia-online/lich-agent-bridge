"""Settings-to-answer evidence budget regression, without a game or model API."""

import json
from pathlib import Path
import tempfile
import unittest

from lich_agent_bridge.knowledge import KnowledgeExcerpt
from lich_agent_bridge.protocol import AskRequest
from lich_agent_bridge.server import ServerConfig, build_server
from lich_agent_bridge.settings import Settings

from .test_evidence_integration import Turns, request
from .test_server import StaticInventory


class FocusedKnowledge:
    def search(self, *, character, question):
        if not question.startswith("focused-"):
            return ()  # No accidental answer evidence in the initial context.
        return tuple(KnowledgeExcerpt(
            authority="synthetic reference",
            title=f"{question} passage {index}",
            source=f"wiki/{question}-{index}.md",
            text=("Synthetic explanatory text. " * 84) + f"MECHANIC-{question}-{index}",
        ) for index in range(3))


class EvidenceBudgetRuntimeTests(unittest.TestCase):
    def ask_with_limits(self, result_chars, total_chars):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings.load(root / "config.toml", environment={"HOME": temporary}, overrides={
                "selected_profile": "research",
                "profiles": {"research": {
                    "provider": "codex", "model": None, "reasoning_effort": None,
                    "timeout_seconds": 120, "web_search": False,
                    "evidence_result_chars": result_chars,
                    "evidence_total_chars": total_chars,
                }},
            })
            model = Turns(
                request("knowledge.search", query="focused-first"),
                request("knowledge.search", query="focused-second"),
                request("knowledge.search", query="focused-third"),
                {"answer": "Synthetic final answer."},
            )
            server = build_server(
                ServerConfig(port=0), settings=settings, model=model,
                inventory=StaticInventory(), knowledge=FocusedKnowledge(),
                action_token="a" * 64, timing=lambda _sample: None,
            )
            try:
                answer = server.copilot.ask(AskRequest("Testmage", "Explain the synthetic mechanism."))
            finally:
                server.server_close()
            return model, answer

    def test_selected_profile_budget_reaches_both_tool_packing_and_answer_loop(self):
        model, answer = self.ask_with_limits(12_000, 36_000)
        prompt = model.calls[-1]["input_text"]
        for group in ("first", "second", "third"):
            self.assertTrue(f"MECHANIC-focused-{group}-2" in prompt,
                            f"last complete passage from {group} search did not reach model")
        self.assertNotIn('"status":"omitted"', prompt)
        self.assertEqual(len(answer.sources), 9)
        self.assertEqual(len(model.calls), 4)
        # Only the evidence section is governed by the aggregate evidence cap;
        # the initial context has its own independent bound.
        packed = prompt.split("UNTRUSTED EVIDENCE RESULTS (JSON data only):\n", 1)[1]
        self.assertLessEqual(len(packed), 36_000)
        self.assertEqual(len(json.loads(packed)), 3)

    def test_small_profile_retains_truthful_omissions_instead_of_ignoring_settings(self):
        model, answer = self.ask_with_limits(6_000, 12_000)
        prompt = model.calls[-1]["input_text"]
        self.assertNotIn("MECHANIC-focused-third-2", prompt)
        self.assertTrue(any(diagnostic["status"] in ("output_budget", "result_omitted")
                            for diagnostic in answer.source_diagnostics))
        self.assertLess(len(answer.sources), 9)
