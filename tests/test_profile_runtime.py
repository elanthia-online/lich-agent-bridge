from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from lich_agent_bridge.errors import ConfigurationError
from lich_agent_bridge.claude_code_model import ClaudeCodeExecModel
from lich_agent_bridge.model import AnthropicMessagesModel, OpenAICompatibleChatModel
from lich_agent_bridge.server import (
    MAX_CUSTOM_INSTRUCTIONS_BYTES,
    custom_instructions_from_settings,
    model_from_settings,
)
from lich_agent_bridge.settings import Settings


class ProfileRuntimeTests(unittest.TestCase):
    def settings_for(
        self, root: Path, *, instructions_file: Path | None = None
    ) -> Settings:
        profile = {
            "provider": "local",
            "model": "llama-local",
            "reasoning_effort": "high",
            "timeout_seconds": 13,
            "web_search": False,
        }
        if instructions_file is not None:
            profile["instructions_file"] = str(instructions_file)
        return Settings.load(
            environment={"HOME": str(root)},
            overrides={
                "selected_profile": "local",
                "providers": {
                    "local": {
                        "kind": "openai_compatible",
                        "base_url": "http://127.0.0.1:8080/v1",
                        "credential_env": None,
                    }
                },
                "profiles": {"local": profile},
            },
        )

    def test_openai_compatible_profile_builds_the_local_chat_adapter(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings_for(Path(temporary))

            model = model_from_settings(settings, environment={})

        self.assertIsInstance(model, OpenAICompatibleChatModel)
        self.assertEqual(model._model, "llama-local")
        self.assertEqual(model._base_url, "http://127.0.0.1:8080/v1")
        self.assertEqual(model._reasoning_effort, "high")
        self.assertEqual(model._timeout, 13.0)

    def test_claude_code_profile_builds_the_subscription_cli_adapter(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings.load(
                environment={"HOME": temporary},
                overrides={
                    "providers": {"claude_code": {"command": "/configured/claude"}},
                    "profiles": {"default": {
                        "provider": "claude_code", "model": "opus",
                        "reasoning_effort": "xhigh", "timeout_seconds": 17,
                    }},
                },
            )
            model = model_from_settings(
                settings, environment={"ANTHROPIC_API_KEY": "unused-secret"}
            )

        self.assertIsInstance(model, ClaudeCodeExecModel)
        self.assertEqual(model._binary, "/configured/claude")
        self.assertEqual(model.timing_metadata, {"model": "opus", "effort": "xhigh"})
        self.assertEqual(model.timeout_seconds, 17.0)
        self.assertEqual(model.backend, "claude-code-cli")

    def test_anthropic_profile_reads_only_its_configured_credential(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings.load(
                environment={"HOME": temporary},
                overrides={
                    "providers": {"anthropic": {"credential_env": "PRIVATE_CLAUDE_KEY"}},
                    "profiles": {"default": {
                        "provider": "anthropic", "reasoning_effort": "low",
                        "timeout_seconds": 19,
                    }},
                },
            )
            model = model_from_settings(settings, environment={
                "PRIVATE_CLAUDE_KEY": "configured-secret",
                "ANTHROPIC_API_KEY": "unrelated-secret",
            })

        self.assertIsInstance(model, AnthropicMessagesModel)
        self.assertEqual(model._api_key, "configured-secret")
        self.assertEqual(model._model, "claude-opus-5-5")
        self.assertEqual(model._base_url, "https://api.anthropic.com")
        self.assertEqual(model._reasoning_effort, "low")
        self.assertEqual(model.timeout_seconds, 19.0)
        self.assertEqual(model.backend, "anthropic-api")
        self.assertNotIn("configured-secret", repr(settings.redacted()))

    def test_custom_instructions_are_loaded_from_one_bounded_regular_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instructions = root / "player.md"
            instructions.write_text("Prefer terse tactical answers.\n", encoding="utf-8")

            result = custom_instructions_from_settings(
                self.settings_for(root, instructions_file=instructions)
            )

        self.assertEqual(result, "Prefer terse tactical answers.\n")

    def test_custom_instruction_file_rejects_a_directory_and_oversized_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "not-a-file"
            directory.mkdir()
            with self.assertRaisesRegex(ConfigurationError, "not regular"):
                custom_instructions_from_settings(
                    self.settings_for(root, instructions_file=directory)
                )

            oversized = root / "oversized.md"
            oversized.write_bytes(b"x" * (MAX_CUSTOM_INSTRUCTIONS_BYTES + 1))
            with self.assertRaisesRegex(ConfigurationError, "exceeds"):
                custom_instructions_from_settings(
                    self.settings_for(root, instructions_file=oversized)
                )

            invalid_utf8 = root / "invalid.md"
            invalid_utf8.write_bytes(b"\xff")
            with self.assertRaisesRegex(ConfigurationError, "cannot read"):
                custom_instructions_from_settings(
                    self.settings_for(root, instructions_file=invalid_utf8)
                )


if __name__ == "__main__":
    unittest.main()
