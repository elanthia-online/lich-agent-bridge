"""Subscription-backed Claude Code CLI adapter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from .cli_model import inference_only_instructions, last_error_line, run_controlled
from .errors import ConfigurationError, ModelError
from .question import QuestionControl


_UNSET = object()
# API credentials would make the CLI bill an API account instead of the
# player's signed-in Claude subscription.
_STRIPPED_CREDENTIALS = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})
# Subscription login state that must survive the parent-session scrub below.
_KEPT_CLAUDE_VARIABLES = frozenset({"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"})


class ClaudeCodeExecModel:
    """Run one inference-only, unpersisted ``claude -p`` turn per question."""

    backend = "claude-code-cli"

    def __init__(
        self,
        *,
        binary: str | None | object = _UNSET,
        model: str | None | object = _UNSET,
        reasoning_effort: str | None = None,
        timeout: float = 120.0,
        runner: Callable[..., Any] = subprocess.run,
    ):
        configured_binary = (
            os.getenv("LAB_CLAUDE_BIN") if binary is _UNSET else binary
        )
        if configured_binary is not None and not isinstance(configured_binary, str):
            raise TypeError("binary must be a string or None")
        if binary is _UNSET and not configured_binary:
            configured_binary = "claude"
        if not configured_binary:
            self._binary = None
        elif "/" in configured_binary:
            self._binary = configured_binary
        else:
            self._binary = shutil.which(configured_binary)
        configured_model = os.getenv("LAB_CLAUDE_MODEL") if model is _UNSET else model
        if configured_model is not None and not isinstance(configured_model, str):
            raise TypeError("model must be a string or None")
        self._model = configured_model
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout
        self._runner = runner

    @property
    def configured(self) -> bool:
        return self._binary is not None

    @property
    def timeout_seconds(self) -> float:
        return self._timeout

    @property
    def timing_metadata(self) -> dict[str, str]:
        return {key: value for key, value in {
            "model": self._model, "effort": self._reasoning_effort
        }.items() if value}

    def respond(self, *, instructions: str, input_text: str) -> str:
        return self.respond_controlled(instructions=instructions, input_text=input_text,
                                       control=QuestionControl(self._timeout))

    def respond_controlled(self, *, instructions: str, input_text: str, control: QuestionControl) -> str:
        if not self._binary:
            raise ConfigurationError(
                "Claude Code CLI was not found; install Claude Code and sign in "
                "with `claude auth login`"
            )

        with tempfile.TemporaryDirectory(prefix="lich-agent-bridge-claude-") as runtime:
            # The system prompt goes through a private file rather than argv so
            # other local users cannot read it from the process table and long
            # instructions cannot exceed the argument-length limit.
            system_prompt_path = Path(runtime) / "system-prompt.txt"
            descriptor = os.open(
                system_prompt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(
                    inference_only_instructions(instructions, product="Claude Code")
                )
            command = self._command(system_prompt_path)
            try:
                runner = run_controlled if self._runner is subprocess.run else self._runner
                extra = {"control": control} if runner is run_controlled else {}
                result = runner(
                    command,
                    input=input_text,
                    text=True,
                    capture_output=True,
                    timeout=min(self._timeout, control.remaining()),
                    cwd=runtime,
                    env=child_environment(os.environ),
                    check=False,
                    **extra,
                )
            except subprocess.TimeoutExpired as error:
                raise ModelError("Claude Code subscription request timed out") from error
            except OSError as error:
                raise ModelError(f"Claude Code CLI could not start: {error}") from error

        control.remaining()
        body = _result_object(result.stdout or "")
        if body is not None and body.get("is_error") is True:
            raise ModelError(f"Claude Code CLI failed: {_result_error(body)}")
        if result.returncode != 0:
            detail = (
                _result_error(body) if body is not None
                else last_error_line(result.stderr or "")
            )
            raise ModelError(f"Claude Code CLI failed: {detail}")
        if body is None:
            raise ModelError("Claude Code produced no readable JSON result")
        text = body.get("result")
        if not isinstance(text, str) or not text.strip():
            raise ModelError("Claude Code produced an empty final answer")
        return text.strip()

    def _command(self, system_prompt_path: Path) -> list[str]:
        # Every flag below was checked against `claude --help` for Claude Code
        # 2.1.322.  `--bare` is deliberately absent: it only accepts
        # ANTHROPIC_API_KEY authentication, which defeats subscription login.
        command = [
            self._binary or "claude",
            "--print",
            "--output-format",
            "json",
            # No built-in tools, MCP servers, slash commands, or skills.
            "--tools",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            # Load no user, project, or local settings files, and disable
            # CLAUDE.md, installed plugins, hooks, and other customizations
            # while keeping subscription authentication.
            "--setting-sources",
            "",
            "--safe-mode",
            "--no-session-persistence",
            # Anything that would ask for permission is denied, never prompted.
            "--permission-prompts",
            "none",
            "--system-prompt-file",
            str(system_prompt_path),
        ]
        if self._model:
            command.extend(("--model", self._model))
        if self._reasoning_effort:
            # Settings validates the value; the CLI only warns on unknown ones.
            command.extend(("--effort", self._reasoning_effort))
        return command


def child_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Return the CLI environment without API credentials or parent-session state.

    When LAB itself runs inside a Claude Code session (for example a developer
    starting ``labctl`` from that terminal), inherited ``CLAUDECODE`` and
    ``CLAUDE_*`` session variables could make the child behave as a nested
    session or switch it into API-key-only mode.
    """

    return {
        key: value
        for key, value in environment.items()
        if key not in _STRIPPED_CREDENTIALS
        and not (
            (key == "CLAUDECODE" or key.startswith("CLAUDE_"))
            and key not in _KEPT_CLAUDE_VARIABLES
        )
    }


def _result_object(stdout: str) -> dict[str, Any] | None:
    """Parse the single JSON result; fall back to its last non-empty line."""
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        pass
    else:
        return value if isinstance(value, dict) else None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def _result_error(body: Mapping[str, Any]) -> str:
    for key in ("result", "subtype", "api_error_status"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[-1][:500]
    return "unknown error"
