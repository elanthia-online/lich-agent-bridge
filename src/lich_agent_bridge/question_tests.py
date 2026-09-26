"""Bounded questions with optional gated recon and private corpus recording."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import quote

from .errors import ValidationError
from .protocol import AskRequest, CharacterRequest


MAX_CORPUS_BYTES = 65_536
MAX_CORPUS_CASES = 20


def validate_question(character: str, question: str) -> AskRequest:
    return AskRequest.from_mapping({"character": character, "question": question})


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("corpus contains a duplicate object key")
        result[key] = value
    return result


def load_corpus(path: Path, character: str) -> list[dict[str, str]]:
    """Validate every case before any server request or output creation."""
    with path.open("rb") as stream:
        raw = stream.read(MAX_CORPUS_BYTES + 1)
    if len(raw) > MAX_CORPUS_BYTES:
        raise ValidationError("corpus exceeds 64 KiB")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValidationError("corpus must contain valid UTF-8 JSON") from error
    if not isinstance(value, dict) or set(value) != {"cases"}:
        raise ValidationError("corpus must contain only the cases key")
    cases = value["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CORPUS_CASES:
        raise ValidationError("corpus must contain between 1 and 20 cases")
    result = []
    identifiers: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "question"}:
            raise ValidationError("each case must contain only id and question")
        identifier = case["id"]
        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or len(identifier) > 128
        ):
            raise ValidationError(
                "case id must be a nonempty string of at most 128 characters"
            )
        if identifier in identifiers:
            raise ValidationError("corpus contains duplicate case ids")
        request = validate_question(character, case["question"])
        identifiers.add(identifier)
        result.append({"id": identifier, "question": request.question})
    return result


class QuestionSession:
    """Pin one character generation; transport callbacks never retry."""

    def __init__(
        self,
        character: str,
        get: Callable[[str], Any],
        post: Callable[[dict[str, Any], float], Any],
        *,
        allow_recon: bool = False,
    ) -> None:
        self.character = CharacterRequest.from_mapping({"character": character}).character
        self.read_only = not allow_recon
        self.generation: str | None = None
        self._get = get
        self._post = post

    def answer(self, question: str) -> dict[str, Any]:
        started = time.monotonic()
        result: dict[str, Any] = {
            "character": self.character,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "read_only": self.read_only,
            "status": "failed",
            "answer": None,
            "sources": [],
            "diagnostics": [],
        }
        try:
            request = validate_question(self.character, question)
            result["question"] = request.question
            health = self._get("/health")
            if (
                not isinstance(health, dict)
                or health.get("service") != "lich-agent-bridge"
                or health.get("status") != "ok"
            ):
                raise ValidationError("sidecar health is not compatible or ready")
            timeout = health.get("ask_timeout_seconds")
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not 1 <= timeout <= 600
                or not math.isfinite(timeout)
            ):
                raise ValidationError("sidecar health has no valid question deadline")
            state = self._get("/v1/state/" + quote(request.character, safe=""))
            if not isinstance(state, dict):
                raise ValidationError("character state is missing or invalid")
            snapshot = state.get("snapshot")
            freshness = state.get("freshness")
            if not isinstance(snapshot, dict) or snapshot.get("character") != request.character:
                raise ValidationError("character state does not match the requested character")
            if not isinstance(freshness, dict) or freshness.get("stale") is not False:
                raise ValidationError("character state is stale or has unknown freshness")
            generation = snapshot.get("generation")
            if not isinstance(generation, str) or not generation.strip():
                raise ValidationError("character state has no valid generation")
            if self.generation is not None and self.generation != generation:
                raise ValidationError("character generation changed; corpus stopped")
            self.generation = generation
            response = self._post(
                {
                    "character": request.character,
                    "question": request.question,
                    "read_only": self.read_only,
                    "expected_generation": generation,
                },
                timeout + 5,
            )
            if not isinstance(response, dict) or response.get("error"):
                raise ValidationError("sidecar returned an error or invalid answer")
            allowed = {"read_only"} if self.read_only else {"read_only", "evidence_gathering"}
            capability = response.get("capability")
            if (
                response.get("character") != request.character
                or not isinstance(capability, str)
                or capability not in allowed
            ):
                raise ValidationError("answer identity or evidence capability did not match")
            answer = response.get("text")
            sources = response.get("sources")
            diagnostics = response.get("source_diagnostics")
            if not isinstance(answer, str) or not answer.strip():
                raise ValidationError("sidecar returned an empty or invalid answer")
            if (
                not isinstance(sources, list)
                or not all(isinstance(item, dict) for item in sources)
                or not isinstance(diagnostics, list)
                or not all(isinstance(item, dict) for item in diagnostics)
            ):
                raise ValidationError("sidecar returned invalid source diagnostics")
            result.update(
                status="answered",
                answer=answer,
                sources=sources,
                diagnostics=diagnostics,
                quality_review="required",
            )
            request_id = response.get("request_id")
            if isinstance(request_id, str) and request_id.strip():
                result["request_id"] = request_id
            observed_count = response.get("observed_event_count")
            if type(observed_count) is int and observed_count >= 0:
                result["observed_event_count"] = observed_count
        except (ValidationError, SystemExit, OSError) as error:
            result["error"] = str(error)
        except KeyboardInterrupt:
            result["error"] = "interrupted; an admitted question may still be running"
        result["generation"] = self.generation
        result["elapsed_ms"] = round((time.monotonic() - started) * 1_000, 3)
        return result


def run_corpus(
    session: QuestionSession, cases: list[dict[str, str]], output: Path
) -> dict[str, Any]:
    """Record transport outcomes, never semantic grades or automatic retries."""
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        results: list[dict[str, Any]] = []
        failed = False
        for case in cases:
            if failed:
                result = {
                    "status": "skipped", "error": "an earlier case failed",
                    "answer": None, "sources": [], "diagnostics": [], "elapsed_ms": 0,
                }
            else:
                result = session.answer(case["question"])
                failed = result["status"] != "answered"
            results.append({**case, **result})
        report = {
            "character": session.character,
            "generation": session.generation,
            "read_only": session.read_only,
            "dialogue": "shared_sequential",
            "quality_review": "required",
            "status": "failed" if failed else "completed",
            "cases": results,
        }
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report
