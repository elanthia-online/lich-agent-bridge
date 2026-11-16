import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lich_agent_bridge.claude_code_model import ClaudeCodeExecModel, child_environment
from lich_agent_bridge.cli_model import inference_only_prompt, run_controlled
from lich_agent_bridge.codex_model import _prompt as codex_prompt
from lich_agent_bridge.codex_model import _run_controlled as codex_run_controlled
from lich_agent_bridge.errors import ConfigurationError, ModelError, QuestionInvalidated
from lich_agent_bridge.question import QuestionControl


def result_json(text="Subscription-backed answer.", **fields):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": text, **fields})


class RecordingRunner:
    def __init__(self, *, returncode=0, stdout=None, stderr=""):
        self.returncode = returncode
        self.stdout = result_json() if stdout is None else stdout
        self.stderr = stderr
        self.command = None
        self.options = None
        self.system_prompt = None
        self.system_prompt_mode = None

    def __call__(self, command, **options):
        self.command = command
        self.options = options
        path = Path(command[command.index("--system-prompt-file") + 1])
        self.system_prompt = path.read_text(encoding="utf-8")
        self.system_prompt_mode = path.stat().st_mode & 0o777
        return subprocess.CompletedProcess(
            command, self.returncode, stdout=self.stdout, stderr=self.stderr
        )


class ClaudeCodeExecModelTests(unittest.TestCase):
    def test_runs_one_isolated_tool_free_unpersisted_print_turn(self):
        runner = RecordingRunner()
        model = ClaudeCodeExecModel(binary="/test/claude", runner=runner)

        answer = model.respond(instructions="Stay read-only.", input_text="Hello")

        self.assertEqual(answer, "Subscription-backed answer.")
        command = runner.command
        self.assertEqual(command[0], "/test/claude")
        self.assertIn("--print", command)
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        self.assertEqual(command[command.index("--permission-prompts") + 1], "none")
        for flag in ("--strict-mcp-config", "--safe-mode", "--disable-slash-commands",
                     "--no-session-persistence"):
            self.assertIn(flag, command)
        # --bare forces API-key authentication and defeats subscription login.
        self.assertNotIn("--bare", command)
        self.assertNotIn("--model", command)
        self.assertNotIn("--effort", command)
        # Instructions travel in a private file, the question on stdin.
        self.assertIn("Stay read-only.", runner.system_prompt)
        self.assertIn("mention Claude Code", runner.system_prompt)
        self.assertNotIn("Stay read-only.", " ".join(command))
        self.assertEqual(runner.system_prompt_mode, 0o600)
        self.assertEqual(runner.options["input"], "Hello")
        runtime = Path(runner.options["cwd"])
        self.assertTrue(runtime.name.startswith("lich-agent-bridge-claude-"))
        self.assertFalse(runtime.exists())

    def test_model_and_effort_are_passed_when_configured(self):
        runner = RecordingRunner()
        model = ClaudeCodeExecModel(binary="/test/claude", model="sonnet",
                                    reasoning_effort="max", runner=runner)
        model.respond(instructions="x", input_text="y")
        self.assertEqual(runner.command[runner.command.index("--model") + 1], "sonnet")
        self.assertEqual(runner.command[runner.command.index("--effort") + 1], "max")
        self.assertEqual(model.timing_metadata, {"model": "sonnet", "effort": "max"})

    def test_child_environment_uses_the_subscription_not_api_credentials(self):
        runner = RecordingRunner()
        ambient = {
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "ANTHROPIC_API_KEY": "must-not-reach-the-cli",
            "ANTHROPIC_AUTH_TOKEN": "must-not-reach-the-cli",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SIMPLE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_OAUTH_TOKEN": "subscription-token",
            "CLAUDE_CONFIG_DIR": "/home/tester/.claude",
        }
        with mock.patch.dict(os.environ, ambient, clear=True):
            ClaudeCodeExecModel(binary="/test/claude", runner=runner).respond(
                instructions="x", input_text="y"
            )
        environment = runner.options["env"]
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE",
                     "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_ENTRYPOINT"):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["CLAUDE_CODE_OAUTH_TOKEN"], "subscription-token")
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], "/home/tester/.claude")
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(child_environment({"OTHER": "kept"}), {"OTHER": "kept"})

    def test_missing_cli_is_not_configured(self):
        model = ClaudeCodeExecModel(binary="")
        self.assertFalse(model.configured)
        with self.assertRaisesRegex(ConfigurationError, "claude auth login"):
            model.respond(instructions="x", input_text="y")

    def test_environment_selects_binary_and_model(self):
        with mock.patch.dict(os.environ, {"LAB_CLAUDE_BIN": "/env/claude",
                                          "LAB_CLAUDE_MODEL": "opus"}):
            model = ClaudeCodeExecModel()
        self.assertTrue(model.configured)
        self.assertEqual(model._binary, "/env/claude")
        self.assertEqual(model.timing_metadata, {"model": "opus"})

    def test_is_error_result_is_a_model_error_even_with_zero_exit(self):
        runner = RecordingRunner(stdout=json.dumps({
            "type": "result", "subtype": "success", "is_error": True,
            "result": "Not logged in · Please run /login",
        }))
        model = ClaudeCodeExecModel(binary="/test/claude", runner=runner)
        with self.assertRaisesRegex(ModelError, "Not logged in"):
            model.respond(instructions="x", input_text="y")

    def test_nonzero_exit_prefers_the_json_error_then_stderr(self):
        runner = RecordingRunner(returncode=1, stdout=json.dumps({
            "type": "result", "subtype": "error_during_execution", "is_error": False,
        }))
        model = ClaudeCodeExecModel(binary="/test/claude", runner=runner)
        with self.assertRaisesRegex(ModelError, "error_during_execution"):
            model.respond(instructions="x", input_text="y")

        runner = RecordingRunner(returncode=1, stdout="", stderr="first\nauth failed\n")
        model = ClaudeCodeExecModel(binary="/test/claude", runner=runner)
        with self.assertRaisesRegex(ModelError, "auth failed"):
            model.respond(instructions="x", input_text="y")

    def test_empty_or_unreadable_output_is_a_model_error(self):
        for stdout, message in (
            (result_json("   "), "empty final answer"),
            (json.dumps({"type": "result", "is_error": False}), "empty final answer"),
            ("not json", "no readable JSON"),
            ("", "no readable JSON"),
            ("[1, 2]", "no readable JSON"),
        ):
            with self.subTest(stdout=stdout):
                model = ClaudeCodeExecModel(
                    binary="/test/claude", runner=RecordingRunner(stdout=stdout)
                )
                with self.assertRaisesRegex(ModelError, message):
                    model.respond(instructions="x", input_text="y")

    def test_result_on_last_line_after_other_output_is_accepted(self):
        stdout = "warning: something noisy\n" + result_json("Last line answer.") + "\n"
        model = ClaudeCodeExecModel(binary="/test/claude",
                                    runner=RecordingRunner(stdout=stdout))
        self.assertEqual(model.respond(instructions="x", input_text="y"),
                         "Last line answer.")

    def test_timeout_is_a_model_error(self):
        def runner(command, **options):
            raise subprocess.TimeoutExpired(command, options["timeout"])
        model = ClaudeCodeExecModel(binary="/test/claude", runner=runner)
        with self.assertRaisesRegex(ModelError, "timed out"):
            model.respond(instructions="x", input_text="y")

    def test_controlled_adapter_uses_remaining_deadline(self):
        runner = RecordingRunner()
        model = ClaudeCodeExecModel(binary="/test/claude", timeout=180, runner=runner)
        model.respond_controlled(instructions="x", input_text="y", control=QuestionControl(2))
        self.assertLessEqual(runner.options["timeout"], 2)
        self.assertEqual(model.timeout_seconds, 180)

    @unittest.skipIf(os.name == "nt", "uses a POSIX executable script")
    def test_invalidated_question_kills_a_real_local_cli_process(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "claude"
            started = Path(directory) / "started"
            script.write_text(
                f"#!{sys.executable}\nimport pathlib, time\n"
                f"pathlib.Path({str(started)!r}).write_text('1')\ntime.sleep(30)\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            model = ClaudeCodeExecModel(binary=str(script), timeout=10)
            control = QuestionControl(10)
            errors = []

            def run():
                try:
                    model.respond_controlled(instructions="x", input_text="y",
                                             control=control)
                except Exception as error:
                    errors.append(error)

            worker = threading.Thread(target=run)
            worker.start()
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(started.exists(), "the fake CLI never started")
            control.cancel("invalidated")
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(errors[0], QuestionInvalidated)


class SharedCliHelperTests(unittest.TestCase):
    def test_codex_keeps_its_helper_aliases_and_exact_prompt(self):
        self.assertIs(codex_run_controlled, run_controlled)
        expected = """INFERENCE-ONLY TASK
Do not use tools, inspect files, run commands, browse, or modify anything.
Return only the response required by COPILOT INSTRUCTIONS, including a JSON
evidence request when that contract is supplied. These requests are data for LAB,
not permission to use native tools. Otherwise return the short private answer.
Do not add progress updates, implementation notes, or mention Codex.

COPILOT INSTRUCTIONS
Be safe.

COPILOT INPUT
Hello
"""
        self.assertEqual(codex_prompt("Be safe.", "Hello"), expected)
        self.assertEqual(
            inference_only_prompt("Be safe.", "Hello", product="Claude Code"),
            expected.replace("mention Codex", "mention Claude Code"),
        )


if __name__ == "__main__":
    unittest.main()
