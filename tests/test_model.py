import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lich_agent_bridge.errors import ConfigurationError, ModelError
from email.message import Message
from urllib.error import HTTPError, URLError

from lich_agent_bridge.model import (
    ANTHROPIC_MAX_TOKENS,
    AnthropicMessagesModel,
    OpenAICompatibleChatModel,
    OpenAIResponsesModel,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RecordingOpener:
    def __init__(self):
        self.request = None
        self.timeout = None

    def __call__(self, request, *, timeout):
        self.request = request
        self.timeout = timeout
        body = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Stay alert."}],
                }
            ],
            "choices": [{"message": {"content": "Stay alert."}}],
        }
        return FakeResponse(json.dumps(body).encode())


class OpenAIResponsesModelTests(unittest.TestCase):
    def test_request_is_stateless_and_has_no_tools(self):
        opener = RecordingOpener()
        model = OpenAIResponsesModel(api_key="test-key", opener=opener)

        answer = model.respond(instructions="Be safe.", input_text="Recent events")

        payload = json.loads(opener.request.data)
        self.assertEqual(opener.request.full_url, "https://api.openai.com/v1/responses")
        self.assertFalse(payload["store"])
        self.assertNotIn("tools", payload)
        self.assertEqual(answer, "Stay alert.")

    def test_missing_key_fails_before_network(self):
        model = OpenAIResponsesModel(api_key="")
        with self.assertRaises(ConfigurationError):
            model.respond(instructions="x", input_text="y")

    def test_reasoning_effort_is_included_when_configured(self):
        opener = RecordingOpener()
        model = OpenAIResponsesModel(
            api_key="test-key", reasoning_effort="low", opener=opener
        )

        model.respond(instructions="Be safe.", input_text="Recent events")

        self.assertEqual(json.loads(opener.request.data)["reasoning"], {"effort": "low"})


class OpenAICompatibleChatModelTests(unittest.TestCase):
    def test_fake_local_server_uses_chat_completions_with_profile_model_and_effort(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib callback name
                received["path"] = self.path
                received["headers"] = dict(self.headers)
                length = int(self.headers["Content-Length"])
                received["payload"] = json.loads(self.rfile.read(length))
                response = json.dumps(
                    {"choices": [{"message": {"content": "Stay alert."}}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            model = OpenAICompatibleChatModel(
                model="llama-local",
                base_url=f"http://{host}:{port}/v1",
                reasoning_effort="medium",
                timeout=7,
            )
            answer = model.respond(
                instructions="Stay safe.", input_text="What happened?"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        payload = received["payload"]
        self.assertEqual(answer, "Stay alert.")
        self.assertEqual(received["path"], "/v1/chat/completions")
        self.assertEqual(payload["model"], "llama-local")
        self.assertEqual(payload["reasoning_effort"], "medium")
        self.assertEqual(payload["messages"][0]["content"], "Stay safe.")
        self.assertEqual(payload["messages"][1]["content"], "What happened?")
        self.assertNotIn("Authorization", received["headers"])

    def test_unavailable_or_malformed_local_server_fails_truthfully(self):
        class MalformedOpener:
            def __call__(self, _request, *, timeout):
                return FakeResponse(b'{"choices": []}')

        malformed = OpenAICompatibleChatModel(
            model="llama-local",
            base_url="http://127.0.0.1:8080/v1",
            opener=MalformedOpener(),
        )
        with self.assertRaisesRegex(ModelError, "no message text"):
            malformed.respond(instructions="x", input_text="y")

        unavailable = OpenAICompatibleChatModel(
            model="llama-local",
            base_url="http://127.0.0.1:8080/v1",
            opener=lambda _request, *, timeout: (_ for _ in ()).throw(OSError("offline")),
        )
        with self.assertRaisesRegex(ModelError, "could not be read"):
            unavailable.respond(instructions="x", input_text="y")


class AnthropicOpener:
    def __init__(self, body=None, error=None):
        self.body = body if body is not None else {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "synthetic"},
                {"type": "text", "text": "Stay "},
                {"type": "text", "text": "alert."},
            ],
            "stop_reason": "end_turn",
        }
        self.error = error
        self.request = None
        self.timeout = None

    def __call__(self, request, *, timeout):
        self.request = request
        self.timeout = timeout
        if self.error is not None:
            raise self.error
        return FakeResponse(json.dumps(self.body).encode())


def anthropic_http_error(code, body):
    return HTTPError(
        "https://api.anthropic.com/v1/messages", code, "reason", Message(),
        io.BytesIO(json.dumps(body).encode()),
    )


class AnthropicMessagesModelTests(unittest.TestCase):
    def test_request_is_one_stateless_tool_free_messages_call(self):
        opener = AnthropicOpener()
        model = AnthropicMessagesModel(api_key="test-key", model=None, opener=opener)

        answer = model.respond(instructions="Be safe.", input_text="Recent events")

        request = opener.request
        payload = json.loads(request.data)
        self.assertEqual(answer, "Stay alert.")
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_method(), "POST")
        headers = {key.casefold(): value for key, value in request.header_items()}
        self.assertEqual(headers["x-api-key"], "test-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertTrue(headers["user-agent"].startswith("lich-agent-bridge/"))
        self.assertNotIn("authorization", headers)
        self.assertEqual(payload, {
            "model": "claude-opus-5-5",
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "system": "Be safe.",
            "messages": [{"role": "user", "content": "Recent events"}],
        })
        self.assertEqual(model.timing_metadata, {"model": "claude-opus-5-5"})

    def test_base_url_model_and_effort_are_honored(self):
        opener = AnthropicOpener()
        model = AnthropicMessagesModel(
            api_key="test-key", model="claude-sonnet-5-5", reasoning_effort="max",
            base_url="http://127.0.0.1:9999/", timeout=7, opener=opener,
        )
        model.respond(instructions="x", input_text="y")
        payload = json.loads(opener.request.data)
        self.assertEqual(opener.request.full_url, "http://127.0.0.1:9999/v1/messages")
        self.assertEqual(payload["model"], "claude-sonnet-5-5")
        self.assertEqual(payload["output_config"], {"effort": "max"})
        self.assertLessEqual(opener.timeout, 7)

    def test_missing_key_fails_before_network(self):
        opener = AnthropicOpener()
        model = AnthropicMessagesModel(api_key="", opener=opener)
        self.assertFalse(model.configured)
        with self.assertRaises(ConfigurationError):
            model.respond(instructions="x", input_text="y")
        self.assertIsNone(opener.request)

    def test_http_errors_report_the_anthropic_error_message_only(self):
        opener = AnthropicOpener(error=anthropic_http_error(401, {
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        }))
        model = AnthropicMessagesModel(api_key="secret-key-value", opener=opener)
        with self.assertRaises(ModelError) as raised:
            model.respond(instructions="x", input_text="y")
        self.assertEqual(
            str(raised.exception), "Anthropic request failed (401): invalid x-api-key"
        )
        self.assertNotIn("secret-key-value", str(raised.exception))

        opener = AnthropicOpener(error=URLError("offline"))
        model = AnthropicMessagesModel(api_key="test-key", opener=opener)
        with self.assertRaisesRegex(ModelError, "Anthropic request failed: offline"):
            model.respond(instructions="x", input_text="y")

    def test_refusal_truncation_and_missing_text_fail_truthfully(self):
        cases = (
            ({"content": [{"type": "text", "text": "partial"}], "stop_reason": "refusal"},
             "declined"),
            ({"content": [{"type": "thinking", "thinking": ""}], "stop_reason": "max_tokens"},
             "max_tokens"),
            ({"content": [{"type": "tool_use", "name": "x", "input": {}}],
              "stop_reason": "tool_use"}, "no text content"),
            ({"content": "not a list"}, "no text content"),
            ([], "not a JSON object"),
        )
        for body, message in cases:
            with self.subTest(message=message):
                model = AnthropicMessagesModel(api_key="test-key",
                                               opener=AnthropicOpener(body=body))
                with self.assertRaisesRegex(ModelError, message):
                    model.respond(instructions="x", input_text="y")

    def test_truncated_answer_with_text_is_returned(self):
        body = {"content": [{"type": "text", "text": "Short answer"}],
                "stop_reason": "max_tokens"}
        model = AnthropicMessagesModel(api_key="test-key", opener=AnthropicOpener(body=body))
        self.assertEqual(model.respond(instructions="x", input_text="y"), "Short answer")


if __name__ == "__main__":
    unittest.main()
