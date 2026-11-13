"""OpenAI and Anthropic HTTP adapters with no third-party runtime dependencies."""

from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import ConfigurationError, ModelError
from .question import QuestionControl

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.6"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
ANTHROPIC_VERSION = "2023-06-01"
# Thinking cannot be disabled on current Claude models and thinking tokens
# count against ``max_tokens``.  The OpenAI adapters' 600-token cap would leave
# little or no room for the visible answer, so this cap is larger; the answer
# length itself is still governed by LAB's instructions.
ANTHROPIC_MAX_TOKENS = 4096
_UNSET = object()


class OpenAIResponsesModel:
    """Translate the model seam into one stateless Responses API call."""

    backend = "openai-api"

    def __init__(
        self,
        *,
        api_key: str | None | object = _UNSET,
        model: str | None | object = _UNSET,
        base_url: str = DEFAULT_BASE_URL,
        reasoning_effort: str | None = None,
        timeout: float = 45.0,
        opener: Callable[..., Any] = urlopen,
    ):
        configured_key = os.getenv("OPENAI_API_KEY") if api_key is _UNSET else api_key
        if configured_key is not None and not isinstance(configured_key, str):
            raise TypeError("api_key must be a string or None")
        configured_model = (
            os.getenv("LAB_MODEL", DEFAULT_MODEL) if model is _UNSET else model
        )
        if configured_model is not None and not isinstance(configured_model, str):
            raise TypeError("model must be a string or None")
        self._api_key = configured_key
        self._model = configured_model or DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout
        self._opener = opener

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

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
        if not self._api_key:
            raise ConfigurationError(
                "OPENAI_API_KEY is not configured in the sidecar environment"
            )
        payload_value: dict[str, Any] = {
            "model": self._model,
            "instructions": instructions,
            "input": input_text,
            "store": False,
            "max_output_tokens": 600,
        }
        if self._reasoning_effort is not None:
            payload_value["reasoning"] = {"effort": self._reasoning_effort}
        payload = json.dumps(payload_value).encode("utf-8")
        request = Request(
            f"{self._base_url}/responses",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "lich-agent-bridge/0.2.0",
            },
        )
        try:
            with self._opener(request, timeout=min(self._timeout, control.remaining())) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = _safe_http_error(error)
            raise ModelError(f"OpenAI request failed ({error.code}): {detail}") from error
        except URLError as error:
            raise ModelError(f"OpenAI request failed: {error.reason}") from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ModelError(f"OpenAI response could not be read: {error}") from error

        control.remaining()
        text = _output_text(body)
        if not text:
            raise ModelError("OpenAI response contained no output text")
        return text


class OpenAICompatibleChatModel:
    """Use the portable Chat Completions contract for local HTTP providers.

    OpenAI-compatible servers such as llama.cpp do not universally expose the
    Responses endpoint, so this adapter intentionally has its own wire
    contract rather than inheriting :class:`OpenAIResponsesModel`.
    """

    backend = "openai-compatible"

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
        reasoning_effort: str | None = None,
        timeout: float = 45.0,
        opener: Callable[..., Any] = urlopen,
    ):
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or None
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout
        self._opener = opener

    @property
    def configured(self) -> bool:
        return bool(self._model and self._base_url)

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
        if not self.configured:
            raise ConfigurationError("OpenAI-compatible provider is not configured")
        payload_value: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": input_text},
            ],
        }
        # This is the Chat Completions reasoning field.  It is deliberately
        # distinct from Responses' `reasoning.effort`; compatible servers that
        # advertise this field can honor it without LAB assuming Responses.
        if self._reasoning_effort is not None:
            payload_value["reasoning_effort"] = self._reasoning_effort
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "lich-agent-bridge/0.2.0",
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload_value).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with self._opener(request, timeout=min(self._timeout, control.remaining())) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = _safe_http_error(error)
            raise ModelError(
                f"OpenAI-compatible request failed ({error.code}): {detail}"
            ) from error
        except URLError as error:
            raise ModelError(
                f"OpenAI-compatible request failed: {error.reason}"
            ) from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ModelError(
                f"OpenAI-compatible response could not be read: {error}"
            ) from error
        control.remaining()
        text = _chat_completion_text(body)
        if not text:
            raise ModelError("OpenAI-compatible response contained no message text")
        return text


class AnthropicMessagesModel:
    """Translate the model seam into one stateless Anthropic Messages call.

    This uses the standard library, like the OpenAI adapters, so LAB keeps no
    third-party runtime dependencies.
    """

    backend = "anthropic-api"

    def __init__(
        self,
        *,
        api_key: str | None | object = _UNSET,
        model: str | None | object = _UNSET,
        base_url: str = DEFAULT_ANTHROPIC_BASE_URL,
        reasoning_effort: str | None = None,
        timeout: float = 45.0,
        opener: Callable[..., Any] = urlopen,
    ):
        configured_key = (
            os.getenv("ANTHROPIC_API_KEY") if api_key is _UNSET else api_key
        )
        if configured_key is not None and not isinstance(configured_key, str):
            raise TypeError("api_key must be a string or None")
        configured_model = (
            os.getenv("LAB_MODEL", DEFAULT_ANTHROPIC_MODEL) if model is _UNSET else model
        )
        if configured_model is not None and not isinstance(configured_model, str):
            raise TypeError("model must be a string or None")
        self._api_key = configured_key
        self._model = configured_model or DEFAULT_ANTHROPIC_MODEL
        self._base_url = base_url.rstrip("/")
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout
        self._opener = opener

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

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
        if not self._api_key:
            raise ConfigurationError(
                "the Anthropic API key is not configured in the sidecar environment"
            )
        payload_value: dict[str, Any] = {
            "model": self._model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "system": instructions,
            "messages": [{"role": "user", "content": input_text}],
        }
        # Effort is a generally available Messages parameter on current
        # models; omitting it keeps the model's own default.
        if self._reasoning_effort is not None:
            payload_value["output_config"] = {"effort": self._reasoning_effort}
        request = Request(
            f"{self._base_url}/v1/messages",
            data=json.dumps(payload_value).encode("utf-8"),
            method="POST",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
                "User-Agent": "lich-agent-bridge/0.2.0",
            },
        )
        try:
            with self._opener(request, timeout=min(self._timeout, control.remaining())) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = _safe_http_error(error)
            raise ModelError(f"Anthropic request failed ({error.code}): {detail}") from error
        except URLError as error:
            raise ModelError(f"Anthropic request failed: {error.reason}") from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ModelError(f"Anthropic response could not be read: {error}") from error

        control.remaining()
        if not isinstance(body, dict):
            raise ModelError("Anthropic response was not a JSON object")
        stop_reason = body.get("stop_reason")
        if stop_reason == "refusal":
            raise ModelError("Anthropic model declined to answer (stop_reason refusal)")
        text = _anthropic_text(body)
        if not text:
            if stop_reason == "max_tokens":
                raise ModelError(
                    "Anthropic response reached max_tokens before any answer text"
                )
            raise ModelError("Anthropic response contained no text content")
        return text


def _output_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    if isinstance(body.get("output_text"), str):
        return body["output_text"].strip()
    chunks: list[str] = []
    for item in body.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks).strip()


def _chat_completion_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if not isinstance(choices, list):
        return ""
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _anthropic_text(body: dict[str, Any]) -> str:
    content = body.get("content")
    if not isinstance(content, list):
        return ""
    chunks = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "".join(chunks).strip()


def _safe_http_error(error: HTTPError) -> str:
    # OpenAI ({"error": {"message"}}) and Anthropic
    # ({"type": "error", "error": {"type", "message"}}) share this shape.
    try:
        body = json.loads(error.read().decode("utf-8"))
        message = body.get("error", {}).get("message")
        if isinstance(message, str) and message:
            return message[:500]
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass
    return error.reason or "unknown error"
