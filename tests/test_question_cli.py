import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from lich_agent_bridge import labctl
from lich_agent_bridge.protocol import MAX_QUESTION_LENGTH
from lich_agent_bridge.question_tests import MAX_CORPUS_BYTES
from lich_agent_bridge.settings import Settings


def state(character="Testchar", generation="session-one", stale=False):
    return {
        "snapshot": {"character": character, "generation": generation},
        "freshness": {"stale": stale},
    }


def answer(**changes):
    return {
        "character": "Testchar",
        "text": "The synthetic observation is incomplete.",
        "capability": "read_only",
        "request_id": "synthetic-request-one",
        "observed_event_count": 2,
        "sources": [{"source": "synthetic fixture"}],
        "source_diagnostics": [{"status": "missing", "source": "skills"}],
        **changes,
    }


class FakeTransport:
    def __init__(self):
        self.requests = []
        self.health = {"service": "lich-agent-bridge", "status": "ok", "ask_timeout_seconds": 180}
        self.states = [state()]
        self.answers = [answer()]
        self.output = None

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        path = urlsplit(request.full_url).path
        if path == "/health":
            result = self.health
        elif path == "/v1/state/Testchar":
            result = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        elif path == "/v1/ask":
            if self.output is not None:
                assert self.output.is_file(), "output must be created before inference"
                assert self.output.stat().st_mode & 0o777 == 0o600
            result = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        else:
            raise AssertionError(f"unexpected request: {path}")
        if isinstance(result, BaseException):
            raise result
        return io.BytesIO(json.dumps(result).encode())

    @property
    def posts(self):
        return [(request, timeout) for request, timeout in self.requests if request.method == "POST"]


class QuestionCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = Settings.load(path=self.root / "missing-config.toml", environment={})
        self.transport = FakeTransport()
        self.stdout = io.StringIO()
        self.addCleanup(patch.stopall)
        patch.object(labctl.Settings, "load", return_value=self.config).start()
        patch.object(labctl, "_token", return_value="synthetic-secret-token").start()
        patch.object(labctl, "urlopen", side_effect=self.transport).start()

    def invoke(self, arguments):
        with redirect_stdout(self.stdout):
            labctl.main(arguments)

    def corpus(self, value=None):
        path = self.root / "private-corpus.json"
        path.write_text(json.dumps(value if value is not None else {"cases": [
            {"id": "first", "question": "What is currently observed?"},
            {"id": "follow-up", "question": "Which observations are missing?"},
        ]}), encoding="utf-8")
        return path

    def run_corpus(self, path=None, *options):
        output = self.root / "private-results.json"
        self.transport.output = output
        self.invoke(["questions", "Testchar", str(path or self.corpus()), "--output", str(output), *options])
        return json.loads(output.read_text())

    def test_ask_uses_authenticated_fresh_state_and_server_deadline(self):
        self.invoke(["ask", "Testchar", "What is observed?"])
        result = json.loads(self.stdout.getvalue())
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], answer()["text"])
        self.assertEqual(result["sources"], answer()["sources"])
        self.assertEqual(result["diagnostics"], answer()["source_diagnostics"])
        self.assertEqual(result["quality_review"], "required")
        self.assertEqual(result["question"], "What is observed?")
        self.assertEqual(result["request_id"], "synthetic-request-one")
        self.assertEqual(result["observed_event_count"], 2)
        self.assertIsNotNone(datetime.fromisoformat(result["started_at"]).utcoffset())
        self.assertGreaterEqual(result["elapsed_ms"], 0)
        request, timeout = self.transport.posts[0]
        self.assertEqual(timeout, 185)
        self.assertEqual(json.loads(request.data), {
            "character": "Testchar", "question": "What is observed?",
            "read_only": True, "expected_generation": "session-one",
        })
        for request, _ in self.transport.requests:
            self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-secret-token")
        self.assertNotIn("synthetic-secret-token", self.stdout.getvalue())

    def test_allow_recon_is_explicit_and_preserved_in_result(self):
        self.transport.answers = [answer(capability="evidence_gathering")]
        self.invoke(["ask", "Testchar", "What is observed?", "--allow-recon"])
        self.assertFalse(json.loads(self.transport.posts[0][0].data)["read_only"])
        self.assertFalse(json.loads(self.stdout.getvalue())["read_only"])

    def test_bad_state_never_posts(self):
        for snapshot in [None, {}, state(character="Otherchar"), state(character="testchar"), state(generation=""), state(generation=None), state(stale=True), state(stale=0), {"snapshot": state()["snapshot"]}]:
            with self.subTest(snapshot=snapshot):
                self.transport.states = [snapshot]
                with self.assertRaises(SystemExit):
                    self.invoke(["ask", "Testchar", "What is observed?"])
                self.assertEqual(self.transport.posts, [])

    def test_invalid_health_deadline_never_posts(self):
        for timeout in [None, True, 0, 601, "120", float("nan"), 10 ** 1000]:
            with self.subTest(timeout=timeout):
                self.transport.health["ask_timeout_seconds"] = timeout
                with self.assertRaises(SystemExit):
                    self.invoke(["ask", "Testchar", "What is observed?"])
                self.assertEqual(self.transport.posts, [])

    def test_incompatible_health_never_posts(self):
        self.transport.health["service"] = "other-listener"
        with self.assertRaises(SystemExit):
            self.invoke(["ask", "Testchar", "What is observed?"])
        self.assertEqual(self.transport.posts, [])

    def test_malformed_corpora_validate_all_cases_before_requests(self):
        valid = {"id": "good", "question": "What is observed?"}
        invalid = [None, [], {}, {"cases": []}, {"cases": [valid], "extra": True},
                   {"cases": [valid, valid]}, {"cases": [valid, {"id": "bad", "question": ""}]},
                   {"cases": [{"id": "bad", "question": 4}]},
                   {"cases": [{"id": "bad", "question": "x" * (MAX_QUESTION_LENGTH + 1)}]},
                   {"cases": [{**valid, "extra": True}]},
                   {"cases": [{"id": str(index), "question": "Question"} for index in range(21)]}]
        for value in invalid:
            with self.subTest(value=value):
                path = self.root / "bad.json"
                path.write_text(json.dumps(value))
                with self.assertRaises(SystemExit):
                    self.run_corpus(path)
                self.assertEqual(self.transport.requests, [])
                self.assertFalse((self.root / "private-results.json").exists())

    def test_duplicate_json_keys_encoding_and_size_are_rejected(self):
        for raw in [b'{"cases":[],"cases":[]}', b'\xff', b"x" * (MAX_CORPUS_BYTES + 1), b"[" * 2000 + b"]" * 2000]:
            with self.subTest(raw_size=len(raw)):
                path = self.root / "bad.json"
                path.write_bytes(raw)
                with self.assertRaises(SystemExit):
                    self.run_corpus(path)
                self.assertEqual(self.transport.requests, [])

    def test_corpus_preserves_order_dialogue_and_private_output(self):
        report = self.run_corpus()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["dialogue"], "shared_sequential")
        self.assertEqual([case["id"] for case in report["cases"]], ["first", "follow-up"])
        self.assertEqual(len(self.transport.posts), 2)
        self.assertEqual([json.loads(request.data)["expected_generation"] for request, _ in self.transport.posts], ["session-one", "session-one"])
        self.assertNotIn("synthetic-secret-token", (self.root / "private-results.json").read_text())

    def test_corpus_recon_opt_in_applies_to_every_case(self):
        self.transport.answers = [answer(capability="evidence_gathering")]
        report = self.run_corpus(None, "--allow-recon")
        self.assertFalse(report["read_only"])
        self.assertTrue(all(not json.loads(request.data)["read_only"] for request, _ in self.transport.posts))

    def test_generation_change_stops_before_next_ask(self):
        self.transport.states = [state(), state(generation="session-two")]
        with self.assertRaises(SystemExit) as error:
            self.run_corpus()
        self.assertEqual(error.exception.code, 1)
        report = json.loads((self.root / "private-results.json").read_text())
        self.assertEqual(report["generation"], "session-one")
        self.assertEqual([case["status"] for case in report["cases"]], ["answered", "failed"])
        self.assertEqual(len(self.transport.posts), 1)

    def test_busy_and_timeouts_are_recorded_without_retry(self):
        for failure in [
            HTTPError("http://localhost/v1/ask", 409, "Busy", {}, io.BytesIO(b'{"error":"question_busy"}')),
            HTTPError("http://localhost/v1/ask", 504, "Timeout", {}, io.BytesIO(b'{"error":"question_timeout"}')),
            HTTPError("http://localhost/v1/ask", 502, "Bad Gateway", {}, io.BytesIO(b'[]')),
            TimeoutError("timed out"), URLError("connection lost"),
        ]:
            with self.subTest(failure=failure):
                output = self.root / "private-results.json"
                if output.exists():
                    output.unlink()
                self.transport.requests.clear()
                self.transport.answers = [failure]
                with self.assertRaises(SystemExit) as error:
                    self.run_corpus()
                self.assertEqual(error.exception.code, 1)
                report = json.loads(output.read_text())
                self.assertEqual([case["status"] for case in report["cases"]], ["failed", "skipped"])
                self.assertEqual(len(self.transport.posts), 1)

    def test_no_clobber_or_symlink_following_before_requests(self):
        output = self.root / "private-results.json"
        output.write_text("existing data")
        with self.assertRaises(SystemExit):
            self.run_corpus()
        self.assertEqual(output.read_text(), "existing data")
        self.assertEqual(self.transport.requests, [])
        output.unlink()
        target = self.root / "target"
        target.write_text("target data")
        output.symlink_to(target)
        with self.assertRaises(SystemExit):
            self.run_corpus()
        self.assertEqual(target.read_text(), "target data")
        self.assertEqual(self.transport.requests, [])

    def test_missing_auth_never_contacts_server(self):
        with patch.object(labctl, "_token", side_effect=SystemExit("cannot read LAB token")):
            with self.assertRaises(SystemExit):
                self.invoke(["ask", "Testchar", "What is observed?"])
        self.assertEqual(self.transport.requests, [])

    def test_invalid_answer_identity_capability_and_payload_fail(self):
        for response in [answer(character="Otherchar"), answer(capability="evidence_gathering"), answer(capability=[]), answer(text=""), answer(sources=None), {"error": "failed"}]:
            with self.subTest(response=response):
                self.transport.answers = [response]
                with self.assertRaises(SystemExit):
                    self.invoke(["ask", "Testchar", "What is observed?"])

    def test_http_200_fallback_is_not_a_quality_pass(self):
        self.transport.answers = [answer(text="The model is unavailable; cached context follows.")]
        self.invoke(["ask", "Testchar", "What is observed?"])
        result = json.loads(self.stdout.getvalue())
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["quality_review"], "required")
        self.assertNotIn("passed", result)


if __name__ == "__main__":
    unittest.main()
