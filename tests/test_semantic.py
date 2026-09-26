import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from lich_agent_bridge import semantic
from lich_agent_bridge.semantic import SemanticPassage, SemanticReranker, SemanticUnavailable


class FakeEncoder:
    cls, sep = -1, -2

    def __init__(self, directory):
        self.batches = []

    def tokenize(self, text):
        return [ord(character) for character in text]

    def encode(self, sequences):
        self.batches.append(sequences)
        return [[1.0, 0.0] for _ in sequences]


def passage(key="one", text="body", title="title", headings=("section",)):
    return SemanticPassage(key, title, headings, text)


class SemanticTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'requires named pipes')
    def test_model_inspection_rejects_nonregular_files_without_opening(self):
        with tempfile.TemporaryDirectory() as directory:
            os.mkfifo(Path(directory) / 'model.onnx')
            with patch.object(Path, 'open', side_effect=AssertionError('must not open a pipe')):
                self.assertEqual(semantic.inspect_model(Path(directory))['status'], 'invalid_model')

    def make(self):
        encoder = FakeEncoder(None)
        return SemanticReranker("/unused", encoder_factory=lambda _: encoder), encoder

    def test_lazy_loading_and_cache_reuses_vectors_but_not_queries(self):
        reranker, encoder = self.make()
        self.assertIsNone(reranker._encoder)
        first = reranker.score("question", [passage()])
        second = reranker.score("different", [passage()])
        self.assertEqual(first.scores, {"one": 1.0})
        self.assertEqual(first.stats["encoded_windows"], 1)
        self.assertEqual(second.stats["cached_windows"], 1)
        self.assertEqual(len(encoder.batches), 3)  # passage once, query twice
        self.assertTrue(all(len(key) == 64 for key in reranker._cache))
        self.assertTrue(all(isinstance(value, list) for value in reranker._cache.values()))

    def test_identity_includes_content_title_headings_and_revision_key(self):
        reranker, _ = self.make()
        for item in (passage(), passage(text="other"), passage(title="other"),
                     passage(headings=("other",)), passage(key="new revision")):
            self.assertEqual(reranker.score("q", [item]).stats["encoded_windows"], 1)
        self.assertEqual(len(reranker._cache), 5)

    def test_complete_body_overlap_prefix_limit_and_batches(self):
        reranker, encoder = self.make()
        body = "".join(chr(1000 + number) for number in range(7000))
        result = reranker.score("q", [passage(text=body, title="t" * 100)])
        batches = encoder.batches[:-1]
        self.assertGreater(result.stats["windows"], 32)
        self.assertTrue(all(len(batch) <= 32 for batch in batches))
        windows = [sequence for batch in batches for sequence in batch]
        self.assertTrue(all(len(sequence) <= 256 for sequence in windows))
        bodies = [sequence[65:-1] for sequence in windows]
        self.assertEqual(set(sum(bodies, [])), set(map(ord, body)))
        self.assertEqual(bodies[0][-32:], bodies[1][:32])
        self.assertEqual(bodies[-1][-1], ord(body[-1]))

    def test_window_cap_includes_cached_input_without_partial_inference(self):
        reranker, encoder = self.make()
        items = [passage(str(index)) for index in range(3)]
        reranker.score("q", items)
        count = len(encoder.batches)
        with patch.object(semantic, "_MAX_WINDOWS", 2):
            with self.assertRaises(SemanticUnavailable) as error:
                reranker.score("q", items)
        self.assertEqual(error.exception.code, "resource_limit")
        self.assertEqual(len(encoder.batches), count)

    def test_character_cap_precedes_model_loading(self):
        factory = lambda _: self.fail("model must not load")
        reranker = SemanticReranker("/unused", encoder_factory=factory)
        with patch.object(semantic, "_MAX_CHARS", 10):
            with self.assertRaises(SemanticUnavailable):
                reranker.score("q", [passage(text="x" * 11)])

    def test_cancellation_after_first_batch_stops_remaining_inference(self):
        reranker, encoder = self.make()
        marker = RuntimeError("cancel after batch")
        def check():
            if encoder.batches:
                raise marker
        with self.assertRaises(RuntimeError) as error:
            reranker.score("q", [passage(text="b" * 9000)], check=check)
        self.assertIs(error.exception, marker)
        self.assertEqual(len(encoder.batches), 1)
        self.assertEqual(len(encoder.batches[0]), 32)
        self.assertEqual(len(reranker._cache), 0)

    def test_missing_optional_dependency_is_typed_without_raw_import_details(self):
        def factory(directory):
            raise ImportError("sensitive module path")
        reranker = SemanticReranker("/unused", encoder_factory=factory)
        with self.assertRaises(SemanticUnavailable) as error:
            reranker.score("q", [passage()])
        self.assertEqual(error.exception.code, "missing_dependencies")
        self.assertNotIn("sensitive", str(error.exception))

    def test_query_cap_does_not_truncate(self):
        reranker, encoder = self.make()
        with self.assertRaises(SemanticUnavailable):
            reranker.score("q" * 255, [passage()])
        self.assertEqual(encoder.batches, [])

    def test_lru_is_window_bounded_and_evicts_least_recent(self):
        reranker, _ = self.make()
        with patch.object(semantic, "_CACHE_WINDOWS", 2):
            reranker.score("q", [passage("a"), passage("b")])
            reranker.score("q", [passage("a")])
            reranker.score("q", [passage("c")])
            self.assertEqual(reranker.score("q", [passage("a")]).stats["cached_windows"], 1)
            self.assertEqual(reranker.score("q", [passage("b")]).stats["encoded_windows"], 1)
            self.assertEqual(len(reranker._cache), 2)

    def test_concurrent_use_falls_back_without_queueing(self):
        reranker, encoder = self.make()
        entered, release = threading.Event(), threading.Event()
        original = encoder.encode
        def blocked(sequences):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(sequences)
        encoder.encode = blocked
        errors = []
        def run():
            try:
                reranker.score("q", [passage()])
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(SemanticUnavailable) as error:
                reranker.score("q", [passage()])
            self.assertEqual(error.exception.code, "busy")
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_callback_exceptions_propagate_and_lock_releases(self):
        for boundary in (1, 3, 4, 5, 6, 7, 8, 9):
            reranker, _ = self.make()
            marker = OSError("cancel marker")
            calls = 0
            def check():
                nonlocal calls
                calls += 1
                if calls == boundary:
                    raise marker
            with self.assertRaises(OSError) as error:
                reranker.score("q", [passage()], check=check)
            self.assertIs(error.exception, marker)
            self.assertEqual(reranker.score("q", [passage()]).scores["one"], 1)

    def test_artifact_validation_and_doctor_without_loading_optional_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(semantic.inspect_model(root)["status"], "invalid_model")
            expected = {"model.onnx": hashlib.sha256(b"model").hexdigest(),
                        "tokenizer.json": hashlib.sha256(b"tokenizer").hexdigest()}
            (root / "model.onnx").write_bytes(b"model")
            (root / "tokenizer.json").write_bytes(b"tokenizer")
            with patch.object(semantic, "ARTIFACTS", expected), \
                 patch.object(importlib.util, "find_spec", return_value=object()), \
                 patch.object(semantic, "_OnnxEncoder", side_effect=AssertionError("no load")):
                report = semantic.inspect_model(root)
                self.assertEqual(report["status"], "ready")
                self.assertEqual(report["artifacts"], {name: True for name in expected})
            with patch.object(semantic, "ARTIFACTS", expected), \
                 patch.object(importlib.util, "find_spec", return_value=None):
                self.assertEqual(semantic.inspect_model(root)["status"], "missing_dependencies")
            with self.assertRaises(SemanticUnavailable) as error:
                SemanticReranker(root).score("q", [passage()])
            self.assertEqual(error.exception.code, "invalid_model")
            self.assertNotIn(directory, str(error.exception))
        self.assertEqual(semantic.inspect_model(None)["status"], "disabled")

    def test_hash_cancellation_is_not_mistaken_for_file_error(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "model.onnx").write_bytes(b"model")
            marker = OSError("cancel hash")
            def check():
                raise marker
            with self.assertRaises(OSError) as error:
                semantic._artifacts(directory, check)
            self.assertIs(error.exception, marker)

    def test_loaded_encoder_is_not_reloaded_and_backend_errors_are_sanitized(self):
        reranker, encoder = self.make()
        reranker.score("q", [passage()])
        reranker._factory = lambda _: self.fail("must not reload")
        encoder.encode = lambda _: (_ for _ in ()).throw(RuntimeError("secret/path"))
        with self.assertRaises(SemanticUnavailable) as error:
            reranker.score("q", [passage()])
        self.assertEqual(error.exception.code, "inference_failed")
        self.assertNotIn("secret", str(error.exception))

    def test_empty_body_still_scores_and_duplicate_keys_fail(self):
        reranker, _ = self.make()
        self.assertEqual(reranker.score("q", [passage(text="")]).scores, {"one": 1.0})
        with self.assertRaises(SemanticUnavailable) as error:
            reranker.score("q", [passage(), passage()])
        self.assertEqual(error.exception.code, "invalid_input")


if __name__ == "__main__":
    unittest.main()
