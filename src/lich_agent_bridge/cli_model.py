"""Shared helpers for subscription-backed, inference-only CLI adapters."""

from __future__ import annotations

import os
import signal
import subprocess
import time


def run_controlled(command, *, input, timeout, control, capture_output, check, **options):
    """Stop the isolated CLI process when its question is invalidated or expires."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=os.name != "nt", **options) as process:
        pending_input = input
        try:
            while True:
                remaining = control.remaining()
                remaining = min(remaining, deadline - time.monotonic())
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(input=pending_input, timeout=min(0.1, remaining))
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    # communicate retains its unsent input across timed-out calls.
                    pending_input = None
        except BaseException:
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise


def inference_only_instructions(instructions: str, *, product: str) -> str:
    """Wrap LAB instructions so a coding-agent CLI answers without acting.

    ``product`` names the CLI so the answer never mentions the tool that
    produced it.
    """

    return f"""INFERENCE-ONLY TASK
Do not use tools, inspect files, run commands, browse, or modify anything.
Return only the response required by COPILOT INSTRUCTIONS, including a JSON
evidence request when that contract is supplied. These requests are data for LAB,
not permission to use native tools. Otherwise return the short private answer.
Do not add progress updates, implementation notes, or mention {product}.

COPILOT INSTRUCTIONS
{instructions}
"""


def inference_only_prompt(instructions: str, input_text: str, *, product: str) -> str:
    """Return the single-document wrapper for CLIs that read one prompt."""

    return f"""{inference_only_instructions(instructions, product=product)}
COPILOT INPUT
{input_text}
"""


def last_error_line(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return (lines[-1] if lines else "unknown error")[:500]
