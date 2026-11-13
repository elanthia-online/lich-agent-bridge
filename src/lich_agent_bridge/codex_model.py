"""Subscription-backed Codex CLI adapter."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .cli_model import inference_only_prompt, last_error_line, run_controlled
from .errors import ConfigurationError, ModelError
from .question import QuestionControl


_UNSET = object()


class CodexExecModel:
    """Run one inference-only, ephemeral Codex exec turn per player question."""

    backend = "codex-cli"

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
            os.getenv("LAB_CODEX_BIN") if binary is _UNSET else binary
        )
        if configured_binary is not None and not isinstance(configured_binary, str):
            raise TypeError("binary must be a string or None")
        if binary is _UNSET and not configured_binary:
            configured_binary = "codex"
        if not configured_binary:
            self._binary = None
        elif "/" in configured_binary:
            self._binary = configured_binary
        else:
            self._binary = shutil.which(configured_binary)
        configured_model = os.getenv("LAB_CODEX_MODEL") if model is _UNSET else model
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
                "Codex CLI was not found; install Codex and sign in with ChatGPT"
            )

        prompt = _prompt(instructions, input_text)
        with tempfile.TemporaryDirectory(prefix="lich-agent-bridge-codex-") as runtime:
            output_path = Path(runtime) / "last-message.txt"
            command = self._command(Path(runtime), output_path)
            environment = os.environ.copy()
            environment.pop("OPENAI_API_KEY", None)
            try:
                runner = _run_controlled if self._runner is subprocess.run else self._runner
                extra = {"control": control} if runner is _run_controlled else {}
                result = runner(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=min(self._timeout, control.remaining()),
                    cwd=runtime,
                    env=environment,
                    check=False,
                    **extra,
                )
            except subprocess.TimeoutExpired as error:
                raise ModelError("Codex subscription request timed out") from error
            except OSError as error:
                raise ModelError(f"Codex CLI could not start: {error}") from error

            control.remaining()
            if result.returncode != 0:
                detail = _last_error_line(result.stderr)
                raise ModelError(f"Codex CLI failed: {detail}")
            try:
                text = output_path.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ModelError(f"Codex produced no readable final answer: {error}") from error
            if not text:
                raise ModelError("Codex produced an empty final answer")
            return text

    def _command(self, runtime: Path, output_path: Path) -> list[str]:
        command = [
            self._binary or "codex",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--cd",
            str(runtime),
            "--config",
            'approval_policy="never"',
        ]
        for feature in (
            "apps",
            "code_mode",
            "hooks",
            "multi_agent",
            "shell_tool",
            "unified_exec",
            "web_search",
        ):
            command.extend(("--disable", feature))
        if self._model:
            command.extend(("--model", self._model))
        if self._reasoning_effort:
            # `model_reasoning_effort` is the installed Codex CLI's TOML
            # configuration key.  Settings validates the value before it
            # reaches this adapter, so this is a literal configuration value,
            # not user-controlled shell syntax.
            command.extend(
                (
                    "--config",
                    f'model_reasoning_effort="{self._reasoning_effort}"',
                )
            )
        command.extend(("--output-last-message", str(output_path), "-"))
        return command


# Compatibility aliases: these helpers are shared with the Claude Code adapter.
_run_controlled = run_controlled
_last_error_line = last_error_line


def _prompt(instructions: str, input_text: str) -> str:
    return inference_only_prompt(instructions, input_text, product="Codex")
