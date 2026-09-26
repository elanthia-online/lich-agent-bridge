"""Optional, offline MiniLM passage scoring; source selection belongs to research.

No optional dependency is imported until first use. Models are pinned, loaded
once, and never downloaded or hot-reloaded. Resource overflow rejects the whole
attempt, allowing the caller to retain its unchanged lexical order.
"""
from __future__ import annotations

from array import array
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
from pathlib import Path
from threading import Lock
import time

ARTIFACTS = {
    "model.onnx": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
    "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
}
MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2@1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
_MAX_WINDOWS = 256
_MAX_CHARS = 200_000
_CACHE_WINDOWS = 4096
_BATCH = 32


class SemanticUnavailable(Exception):
    """A sanitized, explicit reason to use the complete lexical fallback."""

    def __init__(self, code: str, reason: str):
        self.code, self.reason = code, reason
        super().__init__(reason)


@dataclass(frozen=True)
class SemanticPassage:
    key: str
    title: str
    heading_path: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class SemanticScores:
    scores: dict[str, float]
    stats: dict


def _artifacts(directory, check):
    result = {}
    for name, expected in ARTIFACTS.items():
        digest = hashlib.sha256()
        try:
            path = Path(directory) / name
            if not path.is_file():
                result[name] = False
                continue
            stream = path.open("rb")
        except (OSError, TypeError, ValueError):
            result[name] = False
            continue
        valid = True
        with stream:
            while True:
                check()
                try:
                    chunk = stream.read(1024 * 1024)
                except OSError:
                    valid = False
                    break
                if not chunk:
                    break
                digest.update(chunk)
        result[name] = valid and digest.hexdigest() == expected
    return result


def inspect_model(directory: Path | None) -> dict:
    """Doctor-only inspection: dependency discovery and hashes, no model load."""
    if directory is None:
        return {"status": "disabled", "detail": "Optional semantic reranking is disabled."}
    dependencies = {}
    for name in ("numpy", "onnxruntime", "tokenizers"):
        try:
            dependencies[name] = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            dependencies[name] = False
    artifacts = _artifacts(directory, lambda: None)
    status = ("invalid_model" if not all(artifacts.values()) else
              "missing_dependencies" if not all(dependencies.values()) else "ready")
    detail = {"invalid_model": "Pinned local model artifacts are missing or do not match.",
              "missing_dependencies": "Optional semantic dependencies are unavailable.",
              "ready": "Pinned local artifacts and optional dependencies are available; inference not tested."}[status]
    return {"status": status, "detail": detail, "dependencies": dependencies, "artifacts": artifacts}


class _OnnxEncoder:
    """Private adapter: token IDs in, normalized float vectors out."""

    def __init__(self, directory):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        ort.disable_telemetry_events()
        self.np = np
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.cls, self.sep, self.pad = (self.tokenizer.token_to_id(token)
                                       for token in ("[CLS]", "[SEP]", "[PAD]"))
        if None in (self.cls, self.sep, self.pad):
            raise ValueError("invalid special tokens")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        self.session = ort.InferenceSession(str(directory / "model.onnx"),
            sess_options=options, providers=["CPUExecutionProvider"])

    def tokenize(self, text):
        return self.tokenizer.encode(text, add_special_tokens=False).ids

    def encode(self, sequences):
        np = self.np
        width = max(map(len, sequences))
        ids = np.full((len(sequences), width), self.pad, dtype=np.int64)
        mask = np.zeros_like(ids)
        for row, tokens in enumerate(sequences):
            ids[row, :len(tokens)] = tokens
            mask[row, :len(tokens)] = 1
        values = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
        output = self.session.run(None, {item.name: values[item.name]
                                        for item in self.session.get_inputs()})[0]
        if output.ndim != 3 or output.shape[:2] != ids.shape or output.shape[-1] != 384:
            raise ValueError("invalid model output")
        weights = mask[..., None].astype(np.float32)
        pooled = (output * weights).sum(axis=1) / weights.sum(axis=1).clip(min=1)
        vectors = pooled / np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-12)
        if not np.isfinite(vectors).all():
            raise ValueError("nonfinite model output")
        return [array("f", vector) for vector in vectors]


class SemanticReranker:
    """Score complete admitted passages, with bounded, process-local vector reuse.

    The optional factory is a deterministic test seam replacing artifact/backend
    loading. Its adapter provides cls, sep, tokenize(text), encode(token_batches).
    check() may cancel at checkpoints; its exceptions always propagate unchanged.
    """

    def __init__(self, model_directory, *, encoder_factory=None):
        self.directory = Path(model_directory)
        self._factory = encoder_factory
        self._encoder = None
        self._lock = Lock()
        self._cache = OrderedDict()

    @staticmethod
    def _backend(operation):
        try:
            return operation()
        except (ImportError, ModuleNotFoundError):
            raise SemanticUnavailable("missing_dependencies", "Optional semantic dependencies are unavailable.") from None
        except Exception:
            raise SemanticUnavailable("inference_failed", "Local semantic encoding failed.") from None

    def score(self, query, passages, *, check=lambda: None) -> SemanticScores:
        check()
        if not self._lock.acquire(blocking=False):
            raise SemanticUnavailable("busy", "Local semantic encoder is busy.")
        try:
            return self._score(query, passages, check)
        finally:
            self._lock.release()

    def _score(self, query, passages, check):
        started = time.perf_counter()
        passages = tuple(passages)
        keys = [passage.key for passage in passages]
        if any(not key for key in keys) or len(set(keys)) != len(keys):
            raise SemanticUnavailable("invalid_input", "Passage identities must be nonempty and unique.")
        chars = len(query) + sum(len(p.text) + len(p.title) + sum(map(len, p.heading_path)) for p in passages)
        if chars > _MAX_CHARS:
            raise SemanticUnavailable("resource_limit", "Semantic input exceeds the character limit.")
        load_started = time.perf_counter()
        if self._encoder is None:
            check()
            if self._factory is None and not all(_artifacts(self.directory, check).values()):
                raise SemanticUnavailable("invalid_model", "Pinned local model artifacts are missing or do not match.")
            check()
            encoder = self._backend(lambda: (self._factory or _OnnxEncoder)(self.directory))
            check()
            self._encoder = encoder
        load_ms = (time.perf_counter() - load_started) * 1000
        encoder = self._encoder
        query_ids = self._backend(lambda: encoder.tokenize(query))
        check()
        if len(query_ids) > 254:
            raise SemanticUnavailable("resource_limit", "Semantic query exceeds the token limit.")
        plans, total = [], 0
        for passage in passages:
            check()
            prefix = self._backend(lambda: encoder.tokenize(passage.title + "\n" + " / ".join(passage.heading_path)))[:64]
            body = self._backend(lambda: encoder.tokenize(passage.text))
            limit = 254 - len(prefix)
            identity = hashlib.sha256(json.dumps([MODEL_ID, ARTIFACTS, passage.key,
                passage.title, passage.heading_path, passage.text], ensure_ascii=False).encode()).hexdigest()
            windows = []
            for begin in range(0, max(1, len(body)), limit - 32):
                end = min(begin + limit, len(body))
                key = hashlib.sha256(f"{identity}:{begin}:{end}".encode()).hexdigest()
                windows.append((key, [encoder.cls, *prefix, *body[begin:end], encoder.sep]))
                total += 1
                if total > _MAX_WINDOWS:
                    raise SemanticUnavailable("resource_limit", "Semantic input exceeds the window limit.")
                if end == len(body):
                    break
            plans.append((passage.key, windows))
        # Count cached windows too; no partial ranking or inference before this gate.
        missing, available = [], {}
        for _, windows in plans:
            for key, sequence in windows:
                if key in self._cache:
                    available[key] = self._cache[key]
                    self._cache.move_to_end(key)
                else:
                    missing.append((key, sequence))
        for begin in range(0, len(missing), _BATCH):
            check()
            batch = missing[begin:begin + _BATCH]
            vectors = self._backend(lambda: encoder.encode([sequence for _, sequence in batch]))
            check()
            if len(vectors) != len(batch):
                raise SemanticUnavailable("inference_failed", "Local semantic encoding returned an invalid batch.")
            for (key, _), vector in zip(batch, vectors):
                available[key] = vector
                self._cache[key] = vector
                if len(self._cache) > _CACHE_WINDOWS:
                    self._cache.popitem(last=False)
        check()
        query_vectors = self._backend(lambda: encoder.encode([[encoder.cls, *query_ids, encoder.sep]]))
        check()
        if len(query_vectors) != 1:
            raise SemanticUnavailable("inference_failed", "Local semantic encoding returned an invalid query.")
        query_vector = query_vectors[0]
        scores = {}
        for key, windows in plans:
            similarities = []
            for window_key, _ in windows:
                vector = available[window_key]
                if len(vector) != len(query_vector):
                    raise SemanticUnavailable("inference_failed", "Local semantic vector dimensions do not match.")
                similarity = sum(left * right for left, right in zip(vector, query_vector))
                if not math.isfinite(similarity):
                    raise SemanticUnavailable("inference_failed", "Local semantic score is not finite.")
                similarities.append(similarity)
            scores[key] = max(similarities)
        check()
        return SemanticScores(scores, {"windows": total, "cached_windows": total - len(missing),
            "encoded_windows": len(missing), "cache_windows": len(self._cache), "input_chars": chars,
            "model_load_ms": load_ms, "total_ms": (time.perf_counter() - started) * 1000})
