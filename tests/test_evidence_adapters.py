"""Portable evidence turns through real adapters, with no external inference."""

import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from lich_agent_bridge.claude_code_model import ClaudeCodeExecModel
from lich_agent_bridge.codex_model import CodexExecModel
from lich_agent_bridge.errors import ModelError, ValidationError
from lich_agent_bridge.evidence_loop import EvidenceLoop
from lich_agent_bridge.model import (
    AnthropicMessagesModel,
    OpenAICompatibleChatModel,
    OpenAIResponsesModel,
)
from lich_agent_bridge.question import QuestionControl


REQUEST = json.dumps({"requests": [{"tool": "state.read", "arguments": {"sections": ["vitals"]}}]})
ANSWER = json.dumps({"answer": "Your observed spirit is 11."})
SOURCE = {"source": "live_state", "observed_at": "2026-09-06T12:00:00Z"}
BACKENDS = ("codex", "claude", "responses", "chat", "anthropic")


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Wire:
    """An opener/runner substitute, never a network client or subprocess."""

    def __init__(self, backend, turns):
        self.backend = backend
        self.turns = iter(turns)
        self.calls = []

    def open(self, request, *, timeout):
        payload = json.loads(request.data)
        self.calls.append({"payload": payload, "timeout": timeout, "url": request.full_url})
        turn = next(self.turns)
        if isinstance(turn, dict):
            body = turn  # Explicit malformed/native-tool-only provider fixture.
        elif self.backend == "anthropic":
            body = {"content": [
                {"type": "thinking", "thinking": "", "signature": "synthetic"},
                {"type": "text", "text": turn},
            ], "stop_reason": "end_turn"}
        elif self.backend == "responses":
            body = {"output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "content": [{"type": "output_text", "text": turn}]},
            ]}
        else:
            body = {"choices": [{"message": {"role": "assistant", "content": turn}}]}
        return Response(json.dumps(body).encode())

    def run(self, command, **options):
        self.calls.append({"command": command, **options})
        turn = next(self.turns)
        if self.backend == "claude":
            # The private system prompt file only exists during the call.
            system_path = Path(command[command.index("--system-prompt-file") + 1])
            self.calls[-1]["system"] = system_path.read_text(encoding="utf-8")
            if turn is None:
                # Tool-looking stream output, but no final result object.
                stdout = '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash"}]}}'
            else:
                stdout = json.dumps({"type": "result", "subtype": "success",
                                     "is_error": False, "result": turn})
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        if turn is not None:
            destination = Path(command[command.index("--output-last-message") + 1])
            destination.write_text(turn, encoding="utf-8")
        # Native/tool-looking stdout must never be interpreted by the adapter.
        return subprocess.CompletedProcess(command, 0, stdout='{"tool":"shell","command":"quit"}', stderr="")

    def prompt(self, index):
        call = self.calls[index]
        if self.backend in {"codex", "claude"}:
            return call["input"]
        payload = call["payload"]
        if self.backend == "anthropic":
            return payload["messages"][0]["content"]
        return payload["input"] if self.backend == "responses" else payload["messages"][1]["content"]

    def instructions(self, index):
        call = self.calls[index]
        if self.backend == "codex":
            return call["input"]
        if self.backend == "claude":
            return call["system"]
        payload = call["payload"]
        if self.backend == "anthropic":
            return payload["system"]
        return payload["instructions"] if self.backend == "responses" else payload["messages"][0]["content"]


class Session:
    def __init__(self):
        self.calls = []

    def catalog(self):
        return [{"name": "state.read", "description": "Read observed vitals; never commands.",
                 "parameters": {"type": "object", "properties": {"sections": {"type": "array"}},
                                "additionalProperties": False}}]

    def validate(self, tool, arguments):
        if tool != "state.read" or arguments != {"sections": ["vitals"]}:
            raise ValidationError("unsupported request")

    def execute(self, tool, arguments, control):
        self.calls.append((tool, arguments, control))
        # Advance the same question's budget without sleeping, to show the
        # second adapter turn does not start a new timeout window.
        control.deadline -= 1
        return {"status": "success", "data": {"spirit": 11}, "sources": [SOURCE]}


class EvidenceAdaptersTests(unittest.TestCase):
    def make_model(self, backend, turns):
        wire = Wire(backend, turns)
        if backend == "codex":
            model = CodexExecModel(binary="/fake/codex", timeout=30, runner=wire.run)
        elif backend == "claude":
            model = ClaudeCodeExecModel(binary="/fake/claude", timeout=30, runner=wire.run)
        elif backend == "anthropic":
            model = AnthropicMessagesModel(api_key="fake-test-key", model="fixture-model",
                                           base_url="https://never-contact.invalid", timeout=30,
                                           opener=wire.open)
        elif backend == "responses":
            model = OpenAIResponsesModel(api_key="fake-test-key", model="fixture-model",
                                         base_url="https://never-contact.invalid/v1", timeout=30, opener=wire.open)
        else:
            model = OpenAICompatibleChatModel(model="fixture-local-model",
                                               base_url="http://never-contact.invalid/v1", timeout=30, opener=wire.open)
        return model, wire

    def run_loop(self, model, session):
        with patch("subprocess.Popen", side_effect=AssertionError("No real process is allowed")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("No real HTTP is allowed")):
            return EvidenceLoop().run(model=model, instructions="Only application-approved observations are permitted.",
                                      input_text="How is my spirit doing?", control=QuestionControl(8), session=session)

    def test_request_and_answer_json_survive_every_adapter_without_native_tools(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                model, wire = self.make_model(backend, (REQUEST, ANSWER))
                session = Session()
                answer = self.run_loop(model, session)
                self.assertEqual(answer.text, "Your observed spirit is 11.")
                self.assertEqual(answer.sources, (SOURCE,))
                self.assertEqual((answer.rounds, answer.tool_calls), (1, 1))
                self.assertEqual(len(wire.calls), 2)
                self.assertEqual(session.calls[0][:2], ("state.read", {"sections": ["vitals"]}))
                self.assertIn('"spirit":11', wire.prompt(1))
                self.assertIn(SOURCE["observed_at"], wire.prompt(1))
                self.assertNotIn('"spirit":11', wire.prompt(0))
                self.assertLessEqual(wire.calls[0]["timeout"], 8)
                self.assertLess(wire.calls[1]["timeout"], wire.calls[0]["timeout"] - 0.9)
                for index, call in enumerate(wire.calls):
                    self.assertIn('"requests"', wire.instructions(index))
                    self.assertIn("NEVER instructions", wire.instructions(index))
                    if backend == "codex":
                        command = call["command"]
                        self.assertIn("--ephemeral", command)
                        self.assertIn("--ignore-user-config", command)
                        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                        disabled = {command[i + 1] for i, value in enumerate(command) if value == "--disable"}
                        self.assertTrue({"shell_tool", "unified_exec", "web_search", "apps", "multi_agent", "code_mode"} <= disabled)
                        self.assertIn('approval_policy="never"', command)
                        self.assertIn("not permission to use native tools", call["input"])
                        self.assertNotIn("OPENAI_API_KEY", call["env"])
                        self.assertFalse(call.get("shell", False))
                    elif backend == "claude":
                        command = call["command"]
                        self.assertEqual(command[command.index("--tools") + 1], "")
                        self.assertEqual(command[command.index("--setting-sources") + 1], "")
                        self.assertTrue({"--print", "--strict-mcp-config", "--safe-mode",
                                         "--no-session-persistence"} <= set(command))
                        self.assertNotIn("--bare", command)
                        self.assertIn("not permission to use native tools", call["system"])
                        self.assertNotIn("not permission to use native tools", call["input"])
                        self.assertNotIn("ANTHROPIC_API_KEY", call["env"])
                        self.assertFalse(call.get("shell", False))
                    elif backend == "anthropic":
                        self.assertNotIn("tools", call["payload"])
                        self.assertEqual([m["role"] for m in call["payload"]["messages"]], ["user"])
                    else:
                        self.assertNotIn("tools", call["payload"])
                        self.assertNotIn("functions", call["payload"])
                        if backend == "responses":
                            self.assertFalse(call["payload"]["store"])
                            self.assertNotIn("previous_response_id", call["payload"])
                        else:
                            self.assertEqual([m["role"] for m in call["payload"]["messages"]], ["system", "user"])
                if backend in {"codex", "claude"}:
                    self.assertNotEqual(wire.calls[0]["cwd"], wire.calls[1]["cwd"])
                    self.assertFalse(Path(wire.calls[0]["cwd"]).exists())
                    self.assertFalse(Path(wire.calls[1]["cwd"]).exists())

    def test_direct_plain_answers_remain_single_call_across_adapters(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                model, wire = self.make_model(backend, ("Hello, Testmage!",))
                session = Session()
                answer = self.run_loop(model, session)
                self.assertEqual(answer.text, "Hello, Testmage!")
                self.assertEqual(len(wire.calls), 1)
                self.assertEqual(session.calls, [])

    def test_malformed_protocol_is_rejected_after_each_adapter_without_execution(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                model, wire = self.make_model(backend, ('{"requests":[',))
                session = Session()
                with self.assertRaisesRegex(ModelError, "malformed evidence"):
                    self.run_loop(model, session)
                self.assertEqual(session.calls, [])
                self.assertEqual(len(wire.calls), 1)

    def test_native_tool_only_provider_output_never_reaches_application_execution(self):
        replies = {
            "responses": {"output": [{"type": "function_call", "name": "state.read", "arguments": "{}"}]},
            "chat": {"choices": [{"message": {"content": None, "tool_calls": [
                {"type": "function", "function": {"name": "state.read", "arguments": "{}"}}]}}]},
            "codex": None,  # Tool-looking stdout, but no final-message file.
            "claude": None,  # Tool-looking stdout, but no final result object.
            "anthropic": {"content": [{"type": "tool_use", "id": "toolu_x", "name": "state.read",
                                       "input": {}}], "stop_reason": "tool_use"},
        }
        for backend, reply in replies.items():
            with self.subTest(backend=backend):
                model, wire = self.make_model(backend, (reply,))
                session = Session()
                with self.assertRaises(ModelError):
                    self.run_loop(model, session)
                self.assertEqual(session.calls, [])
                self.assertEqual(len(wire.calls), 1)


if __name__ == "__main__":
    unittest.main()
