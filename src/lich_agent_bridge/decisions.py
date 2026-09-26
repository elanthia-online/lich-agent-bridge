"""Typed, fenced Jev decisions for LAB shadow evaluation.

There is intentionally no action broker, command callback, or raw-command field
in this module. Every outcome in this slice has ``executed=False``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import ConfigurationError, ValidationError
from .decision_context import structure_request, validate_context
from .protocol import CharacterSnapshot
from .settings import DecisionProviderKind, DecisionSettings, Settings

MAX_RESPONSE_BYTES = 1_048_576
MAX_OBSERVATION_BYTES = 65_536
MAX_AUDIT_RECORD_BYTES = 65_535
WIRE_ROUNDING_TOLERANCE = 0.0100001
_AUDIT_LOCK = threading.Lock()
PLAYER_ACTIONS = frozenset({"attack_target", "buff_ally", "wait", "retreat"})
SUPERVISOR_RESPONSES = frozenset({
    "continue", "hold", "recover_self", "recover_team", "return_field",
    "return_town", "abort", "escalate", "request_human",
})
PLAYER_CRITERIA = {
    "attack_target": "Use the profile attack on the selected visible target.",
    "buff_ally": "Use the profile ally buff on the party member who most needs it.",
    "wait": "Take no new action during this observation.",
    "retreat": "Stop acquiring targets and return toward the refuge.",
}
SUPERVISOR_CRITERIA = {
    "continue": "Allow the deterministic controller to continue its plan.",
    "hold": "Remain in the locally established safe hold.",
    "recover_self": "Use the deterministic self-recovery path.",
    "recover_team": "Use the deterministic team-recovery path.",
    "return_field": "Return to the configured field refuge.",
    "return_town": "Return using the configured town recovery path.",
    "abort": "Abort the current bounded phase.",
    "escalate": "Escalate the incident to the strategist.",
    "request_human": "Request human judgment without taking another action.",
}


class DecisionError(Exception):
    """The provider failed or returned an invalid answer."""


class DecisionRole(StrEnum):
    SUPERVISOR = "supervisor"
    PLAYER = "player"


class ExecutionPosture(StrEnum):
    SHADOW = "shadow"
    LIVE = "live"


class OutcomeStatus(StrEnum):
    RECOMMENDED = "recommended"
    REJECTED = "rejected"
    PROVIDER_ERROR = "provider_error"
    UNSUPPORTED = "unsupported"


class IncidentKind(StrEnum):
    DANGEROUS_WOUND = "dangerous_wound"
    HEALTH_TRANSITION = "health_transition"
    TEAM_SPLIT = "team_split"
    CONTROLLER_EXIT = "controller_exit"
    CONTROLLER_STALLED = "controller_stalled"
    RECOVERY_FAILED = "recovery_failed"
    EQUIPMENT_FAILURE = "equipment_failure"
    BUFF_FAILURE = "buff_failure"
    ITEM_UPKEEP_FAILURE = "item_upkeep_failure"
    UNKNOWN_BLOCKER = "unknown_blocker"
    NO_PROGRESS = "no_progress"


class IncidentSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"
    CRITICAL = "critical"


class IncidentState(StrEnum):
    DETECTED = "detected"
    SAFE_HOLD = "safe_hold"
    CLASSIFIED = "classified"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class DecisionFence:
    generation: str
    sequence: int
    room_id: str | None
    target_ids: tuple[str, ...]
    plan_id: str
    plan_revision: int
    incident_version: int | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "generation": self.generation, "sequence": self.sequence,
            "room_id": self.room_id, "target_ids": list(self.target_ids),
            "plan_id": self.plan_id, "plan_revision": self.plan_revision,
            "incident_version": self.incident_version,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DecisionFence":
        value = _mapping(value, "source_fence")
        _fields(value, "source_fence", {
            "generation", "sequence", "room_id", "target_ids", "plan_id",
            "plan_revision",
        }, {"incident_version"})
        targets = tuple(_text(item, "target id", 64) for item in _sequence(value["target_ids"], "target_ids"))
        if len(targets) > 32 or len(set(targets)) != len(targets):
            raise ValidationError("source_fence.target_ids must contain at most 32 unique ids")
        room = value["room_id"]
        version = value.get("incident_version")
        return cls(
            generation=_text(value["generation"], "generation", 128),
            sequence=_integer(value["sequence"], "sequence", 0),
            room_id=None if room is None else _text(str(room), "room_id", 64),
            target_ids=tuple(sorted(targets)),
            plan_id=_text(value["plan_id"], "plan_id", 128),
            plan_revision=_integer(value["plan_revision"], "plan_revision", 1),
            incident_version=None if version is None else _integer(version, "incident_version", 1),
        )


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    state: Mapping[str, Any]
    questions: Mapping[str, Mapping[str, Any]]
    model: str

    def to_mapping(self) -> dict[str, Any]:
        return {"state": dict(self.state), "model": self.model,
                "questions": {key: dict(value) for key, value in self.questions.items()}}


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    model: str
    answers: Mapping[str, Mapping[str, Any]]
    usage: Mapping[str, int | float]


class DecisionProvider(Protocol):
    def evaluate(self, request: DecisionRequest) -> DecisionResponse: ...


class NoRedirectHandler(HTTPRedirectHandler):
    """Turn every HTTP redirect into an error without forwarding headers."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class SystemOneDecisionProvider:
    def __init__(self, settings: DecisionSettings, *, environment: Mapping[str, str] | None = None,
                 opener: Callable[..., Any] | None = None) -> None:
        if settings.kind is not DecisionProviderKind.SYSTEM_ONE:
            raise ConfigurationError(f"unsupported decision provider: {settings.kind}")
        if not settings.enabled:
            raise ConfigurationError("decision provider is disabled")
        env = os.environ if environment is None else environment
        credential = None if settings.credential_env is None else env.get(settings.credential_env, "").strip()
        if settings.credential_env is not None and not credential:
            raise ConfigurationError(f"decision credential environment variable {settings.credential_env} is not set")
        self._endpoint = f"{settings.base_url}{settings.endpoint_path}"
        self._credential = credential
        self._timeout = settings.timeout_seconds
        self._opener = build_opener(NoRedirectHandler()).open if opener is None else opener

    def evaluate(self, request: DecisionRequest) -> DecisionResponse:
        body = json.dumps(request.to_mapping(), separators=(",", ":"), ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "lich-agent-bridge/system-one"}
        if self._credential is not None:
            headers["Authorization"] = f"Bearer {self._credential}"
        wire = Request(self._endpoint, data=body, headers=headers, method="POST")
        try:
            with self._opener(wire, timeout=self._timeout) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            detail = {401: "authentication rejected", 422: "request rejected by provider validation",
                      429: "provider temporarily unavailable", 529: "provider temporarily unavailable"}.get(
                          error.code, f"provider returned HTTP {error.code}")
            raise DecisionError(detail) from error
        except (TimeoutError, URLError, OSError) as error:
            raise DecisionError("provider request failed or timed out") from error
        if len(payload) > MAX_RESPONSE_BYTES:
            raise DecisionError("provider response exceeded the size limit")
        try:
            value = json.loads(payload, object_pairs_hook=_unique_json_object)
            return _validate_response(value, request.questions)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
            raise DecisionError("provider returned invalid JSON") from error
        except RecursionError as error:
            raise DecisionError("provider response exceeded the nesting limit") from error


@dataclass(frozen=True, slots=True)
class PlayerFrame:
    snapshot: CharacterSnapshot
    plan_id: str
    plan_revision: int
    plan_step: str
    authorized: tuple[str, ...]
    targets: tuple[Mapping[str, Any], ...]
    party: tuple[Mapping[str, Any], ...]
    recent: tuple[Mapping[str, Any], ...]
    cooldowns: Mapping[str, Any]
    last_op: Mapping[str, Any] | None
    decision_context: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PlayerFrame":
        value = _mapping(value, "player frame")
        _fields(value, "player frame", {"snapshot", "plan"}, {"targets", "party", "recent", "cooldowns", "last_op", "decision_context"})
        snapshot = CharacterSnapshot.from_mapping(_mapping(value["snapshot"], "snapshot"))
        plan = _mapping(value["plan"], "plan")
        _fields(plan, "plan", {"id", "revision", "step", "authorized"})
        authorized = tuple(_text(item, "authorized action", 64) for item in _sequence(plan["authorized"], "authorized"))
        if len(authorized) < 2 or len(set(authorized)) != len(authorized):
            raise ValidationError("plan.authorized must contain at least two unique actions")
        unknown = sorted(set(authorized) - PLAYER_ACTIONS)
        if unknown:
            raise ValidationError(f"unknown player action {unknown[0]!r}")
        targets_raw = value.get("targets")
        if targets_raw is None:
            targets_raw = (snapshot.nearby or {}).get("creatures", [])
        targets = _objects(targets_raw, "targets", 32)
        if len({_target_id(item) for item in targets}) != len(targets):
            raise ValidationError("targets must contain unique ids")
        last_op = value.get("last_op")
        return cls(snapshot, _text(plan["id"], "plan.id", 128),
                   _integer(plan["revision"], "plan.revision", 1), _text(plan["step"], "plan.step", 64),
                   authorized, targets, _objects(value.get("party", []), "party", 16),
                   _objects(value.get("recent", []), "recent", 64),
                   _bounded_mapping(value.get("cooldowns", {}), "cooldowns"),
                   None if last_op is None else _bounded_mapping(last_op, "last_op"),
                   validate_context(value.get("decision_context", {})))

    @property
    def observation_id(self) -> str:
        return f"player:{self.plan_id}:{self.plan_revision}:{self.snapshot.generation}:{self.snapshot.sequence}"

    @property
    def fence(self) -> DecisionFence:
        room = None if self.snapshot.room is None else self.snapshot.room.get("id")
        return DecisionFence(self.snapshot.generation, self.snapshot.sequence, room,
                             tuple(sorted(_target_id(item) for item in self.targets)),
                             self.plan_id, self.plan_revision)

    @property
    def allowed_responses(self) -> tuple[str, ...]:
        return self.authorized

    def request(self, model: str) -> DecisionRequest:
        snap = self.snapshot
        state = {
            "role": "player", "observation_id": self.observation_id, "fence": self.fence.to_mapping(),
            "observed_at": snap.observed_at, "sequence": snap.sequence, "room": snap.room,
            "self": {"vitals": snap.vitals, "stance": snap.stance, "roundtime": snap.roundtime,
                     "stunned": snap.stunned, "dead": snap.dead, "hands": snap.hands,
                     "wounds": snap.wounds, "active_spells": snap.active_spells},
            "targets": list(self.targets), "party": list(self.party), "recent": list(self.recent),
            "plan": {"id": self.plan_id, "revision": self.plan_revision, "step": self.plan_step,
                     "authorized": list(self.authorized)},
            "cooldowns": dict(self.cooldowns), "last_op": None if self.last_op is None else dict(self.last_op),
            "decision_context": dict(self.decision_context),
            "exact": {"dead": snap.dead, "roundtime_blocking": None if snap.roundtime is None else snap.roundtime > 0,
                      "target_count": len(self.targets)},
        }
        questions: dict[str, Mapping[str, Any]] = {
            "next": {"type": "choice", "instructions": "Which authorized symbolic action best advances the current plan step? Exact facts in state.exact are authoritative.",
                     "criteria": {name: PLAYER_CRITERIA[name] for name in self.authorized}},
            "in_danger": {"type": "noul", "instructions": "Should the character stop acquiring targets and begin returning now?",
                          "criteria": {"true": "Return now", "false": "Continue the current step"}},
            "ally_needs_help": {"type": "noul", "instructions": "Is a party member in more immediate danger than this character?"},
            "stuck": {"type": "noul", "instructions": "Do recent events show no progress toward the current plan step?"},
            "need_strategist": {"type": "noul", "instructions": "Is this situation not adequately covered by the authorized symbolic actions?"},
            "urgency": {"type": "score", "instructions": "How urgent is the situation?",
                        "criteria": ["idle", "routine", "pressing", "emergency"]},
        }
        if len(self.targets) > 1:
            questions["target_priority"] = {"type": "choice", "instructions": "Which visible target should be prioritized?",
                "criteria": {_target_id(target): {key: target.get(key) for key in ("noun", "name", "health", "status", "held")
                                                   if target.get(key) is not None} for target in self.targets}}
        return DecisionRequest(state, questions, model)


@dataclass(frozen=True, slots=True)
class IncidentObservation:
    deduplication_key: str
    kind: IncidentKind
    severity: IncidentSeverity
    state: IncidentState
    source_fence: DecisionFence
    affected_characters: tuple[str, ...]
    allowed_responses: tuple[str, ...]
    observed_at: str
    expires_at: str
    facts: Mapping[str, Any]
    decision_context: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "IncidentObservation":
        value = _mapping(value, "incident observation")
        required = {"deduplication_key", "kind", "severity", "state", "source_fence", "affected_characters",
                    "allowed_responses", "observed_at", "expires_at", "facts"}
        _fields(value, "incident observation", required, {"decision_context"})
        fence = DecisionFence.from_mapping(_mapping(value["source_fence"], "source_fence"))
        if fence.incident_version is not None:
            raise ValidationError("incident observation cannot set incident_version")
        affected = tuple(_text(item, "affected character", 40) for item in _sequence(value["affected_characters"], "affected_characters"))
        if not affected or len(affected) > 16 or len(set(affected)) != len(affected):
            raise ValidationError("affected_characters must contain 1 to 16 unique names")
        responses = tuple(_text(item, "allowed response", 32) for item in _sequence(value["allowed_responses"], "allowed_responses"))
        unknown = sorted(set(responses) - SUPERVISOR_RESPONSES)
        if len(responses) < 2 or len(set(responses)) != len(responses) or unknown:
            raise ValidationError(
                "invalid supervisor responses"
                f"{': ' + unknown[0] if unknown else '; at least two are required'}"
            )
        observed_at, expires_at = _timestamp(value["observed_at"]), _timestamp(value["expires_at"])
        if _parse_timestamp(expires_at) <= _parse_timestamp(observed_at):
            raise ValidationError("expires_at must be later than observed_at")
        facts = _bounded_mapping(value["facts"], "facts")
        if len(_canonical(facts)) > 16_384:
            raise ValidationError("facts exceed the incident size limit")
        return cls(_text(value["deduplication_key"], "deduplication_key", 128),
                   _enum(IncidentKind, value["kind"], "kind"),
                   _enum(IncidentSeverity, value["severity"], "severity"),
                   _enum(IncidentState, value["state"], "state"), fence, affected, responses,
                   observed_at, expires_at, facts, validate_context(value.get("decision_context", {})))


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    incident_id: str
    version: int
    observation: IncidentObservation

    @classmethod
    def detected(cls, observation: IncidentObservation) -> "IncidentRecord":
        if observation.state is not IncidentState.DETECTED:
            raise ValidationError("new incident must start in detected state")
        digest = hashlib.sha256(observation.deduplication_key.encode()).hexdigest()[:24]
        return cls(f"inc-{digest}", 1, observation)

    def update_duplicate(self, observation: IncidentObservation) -> "IncidentRecord":
        if observation.deduplication_key != self.observation.deduplication_key:
            raise ValidationError("duplicate incident key does not match")
        if _parse_timestamp(observation.observed_at) <= _parse_timestamp(self.observation.observed_at):
            raise ValidationError("duplicate incident observation must be newer")
        current_fence = self.observation.source_fence
        next_fence = observation.source_fence
        if (
            next_fence.generation != current_fence.generation
            or next_fence.plan_id != current_fence.plan_id
            or next_fence.plan_revision != current_fence.plan_revision
        ):
            raise ValidationError("duplicate incident source identity changed")
        if next_fence.sequence <= current_fence.sequence:
            raise ValidationError("duplicate incident source sequence must increase")
        allowed_transitions = {
            IncidentState.DETECTED: {IncidentState.DETECTED, IncidentState.SAFE_HOLD},
            IncidentState.SAFE_HOLD: {IncidentState.SAFE_HOLD, IncidentState.CLASSIFIED, IncidentState.RESOLVED},
            IncidentState.CLASSIFIED: {IncidentState.CLASSIFIED, IncidentState.RESOLVED},
            IncidentState.RESOLVED: {IncidentState.RESOLVED},
        }
        if observation.state not in allowed_transitions[self.observation.state]:
            raise ValidationError("invalid incident state transition")
        return replace(self, version=self.version + 1, observation=observation)

    @property
    def observation_id(self) -> str:
        return f"{self.incident_id}:v{self.version}"

    @property
    def fence(self) -> DecisionFence:
        return replace(self.observation.source_fence, incident_version=self.version)

    @property
    def allowed_responses(self) -> tuple[str, ...]:
        return self.observation.allowed_responses

    def request(self, model: str) -> DecisionRequest:
        obs = self.observation
        state = {"role": "supervisor", "observation_id": self.observation_id,
                 "incident_id": self.incident_id, "incident_version": self.version,
                 "kind": obs.kind.value, "severity": obs.severity.value, "state": obs.state.value,
                 "source_fence": self.fence.to_mapping(), "affected_characters": list(obs.affected_characters),
                 "observed_at": obs.observed_at, "expires_at": obs.expires_at, "facts": dict(obs.facts),
                 "decision_context": dict(obs.decision_context)}
        question = {"response": {"type": "choice",
                    "instructions": "Which allowed symbolic response best addresses this incident after local safe hold?",
                    "criteria": {name: SUPERVISOR_CRITERIA[name] for name in obs.allowed_responses}}}
        return DecisionRequest(state, question, model)


@dataclass(frozen=True, slots=True)
class DecisionInvocation:
    """Complete input to the single public decision seam."""

    role: DecisionRole
    execution: ExecutionPosture
    observation: PlayerFrame | IncidentRecord
    allowed_responses: tuple[str, ...]
    source_fence: DecisionFence
    expires_at_monotonic: float
    provider: DecisionProviderKind
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, DecisionRole):
            raise ValidationError("unknown decision role")
        if not isinstance(self.execution, ExecutionPosture):
            raise ValidationError("unknown execution posture")
        if not isinstance(self.provider, DecisionProviderKind):
            raise ValidationError("unknown decision provider")
        if not math.isfinite(self.expires_at_monotonic):
            raise ValidationError("decision expiry must be finite")
        _text(self.model, "model", 128)
        expected_type = PlayerFrame if self.role is DecisionRole.PLAYER else IncidentRecord
        vocabulary = PLAYER_ACTIONS if self.role is DecisionRole.PLAYER else SUPERVISOR_RESPONSES
        if not isinstance(self.observation, expected_type):
            raise ValidationError(f"{self.role.value} invocation has the wrong observation type")
        if not self.allowed_responses or len(set(self.allowed_responses)) != len(self.allowed_responses):
            raise ValidationError("allowed_responses must contain unique values")
        unknown = sorted(set(self.allowed_responses) - vocabulary)
        if unknown:
            raise ValidationError(f"unknown {self.role.value} response {unknown[0]!r}")
        if self.source_fence != self.observation.fence:
            raise ValidationError("invocation source fence does not match observation")
        if self.allowed_responses != self.observation.allowed_responses:
            raise ValidationError("invocation responses do not match observation")

    @classmethod
    def player(cls, frame: PlayerFrame, settings: DecisionSettings, *,
               execution: ExecutionPosture = ExecutionPosture.SHADOW,
               monotonic: Callable[[], float] = time.monotonic) -> "DecisionInvocation":
        return cls(DecisionRole.PLAYER, execution, frame, frame.authorized, frame.fence,
                   monotonic() + settings.decision_ttl_seconds, settings.kind, settings.model)

    @classmethod
    def supervisor(cls, incident: IncidentRecord, settings: DecisionSettings, *,
                   execution: ExecutionPosture = ExecutionPosture.SHADOW,
                   monotonic: Callable[[], float] = time.monotonic) -> "DecisionInvocation":
        return cls(DecisionRole.SUPERVISOR, execution, incident, incident.allowed_responses, incident.fence,
                   monotonic() + settings.decision_ttl_seconds, settings.kind, settings.model)

    @property
    def observation_id(self) -> str:
        return self.observation.observation_id

    def request(self) -> DecisionRequest:
        return self.observation.request(self.model)


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    status: OutcomeStatus
    role: DecisionRole
    execution: ExecutionPosture
    observation_id: str
    fence: DecisionFence
    request_sha256: str
    latency_ms: float
    provider: str
    requested_model: str
    model: str | None = None
    reason: str | None = None
    recommendation: str | None = None
    selected_target_id: str | None = None
    confidence: float | None = None
    answers: Mapping[str, Mapping[str, Any]] | None = None
    usage: Mapping[str, int | float] | None = None
    executed: bool = False
    request_format: str = "compact"
    request_bytes: int = 0
    observation_mode: str = "runtime"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status.value, "role": self.role.value, "execution": self.execution.value,
            "observation_id": self.observation_id, "fence": self.fence.to_mapping(),
            "request_sha256": self.request_sha256, "latency_ms": self.latency_ms,
            "provider": self.provider, "requested_model": self.requested_model, "model": self.model,
            "reason": self.reason, "recommendation": self.recommendation,
            "selected_target_id": self.selected_target_id, "confidence": self.confidence,
            "answers": None if self.answers is None else {key: dict(value) for key, value in self.answers.items()},
            "usage": None if self.usage is None else dict(self.usage), "executed": False,
            "request_format": self.request_format, "request_bytes": self.request_bytes,
            "observation_mode": self.observation_mode,
        }


class DecisionToolbox:
    """Evaluate one typed invocation without exposing any execution seam."""

    def __init__(self, provider: DecisionProvider, settings: DecisionSettings, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 audit_log: Path | None = None, request_format: str = "compact") -> None:
        self._provider = provider
        self._settings = settings
        self._monotonic = monotonic
        self._now = now
        self._audit_log = settings.audit_log if audit_log is None else audit_log
        if request_format not in ("compact", "baseline"):
            raise ValidationError("request_format must be compact or baseline")
        self._request_format = request_format

    def evaluate(self, invocation: DecisionInvocation, *,
                 current_fence: Callable[[], DecisionFence]) -> DecisionOutcome:
        """Evaluate runtime state, requiring a fresh fence after inference."""
        return self._evaluate(invocation, current_fence=current_fence)

    def evaluate_replay(self, invocation: DecisionInvocation) -> DecisionOutcome:
        """Evaluate an immutable recorded observation using its recorded fence."""
        return self._evaluate(
            invocation, current_fence=lambda: invocation.source_fence, replay=True
        )

    def _evaluate(self, invocation: DecisionInvocation, *,
                  current_fence: Callable[[], DecisionFence], replay: bool = False) -> DecisionOutcome:
        started = self._monotonic()
        # The audited digest identifies the actual provider payload, not the
        # larger source observation. Invalid/oversize inputs never reach it.
        request = structure_request(invocation.request(), request_format=self._request_format)
        digest = hashlib.sha256(_canonical(request.to_mapping())).hexdigest()
        request_bytes = len(_canonical(request.to_mapping()))

        def finish(status: OutcomeStatus, *, reason: str | None = None,
                   response: DecisionResponse | None = None,
                   recommendation: str | None = None) -> DecisionOutcome:
            primary = None if response is None else _primary(response, invocation.role)
            target = None
            if response is not None and isinstance(invocation.observation, PlayerFrame):
                target = _selected_target(response, invocation.observation.targets)
            outcome = DecisionOutcome(
                status, invocation.role, invocation.execution, invocation.observation_id,
                invocation.source_fence, digest,
                round(max(0.0, self._monotonic() - started) * 1000, 3),
                invocation.provider.value, invocation.model,
                None if response is None else response.model, reason, recommendation, target,
                None if primary is None else primary.get("confidence"),
                None if response is None else response.answers,
                None if response is None else response.usage,
                request_format=self._request_format, request_bytes=request_bytes,
                observation_mode="replay" if replay else "runtime",
            )
            append_decision_audit(self._audit_log, outcome.to_mapping())
            return outcome

        # Live is structurally unavailable and rejected before provider evaluation.
        if invocation.execution is ExecutionPosture.LIVE:
            return finish(OutcomeStatus.UNSUPPORTED, reason="live_execution_not_installed")
        if not self._settings.enabled:
            return finish(OutcomeStatus.UNSUPPORTED, reason="decision_provider_disabled")
        if invocation.provider is not self._settings.kind or invocation.model != self._settings.model:
            return finish(OutcomeStatus.REJECTED, reason="provider_selection_changed")
        if started > invocation.expires_at_monotonic:
            return finish(OutcomeStatus.REJECTED, reason="decision_expired")
        if current_fence() != invocation.source_fence:
            return finish(OutcomeStatus.REJECTED, reason="state_fence_changed")
        if invocation.observation.fence != invocation.source_fence:
            return finish(OutcomeStatus.REJECTED, reason="observation_fence_changed")
        if not replay and isinstance(invocation.observation, PlayerFrame):
            age = (self._now() - _parse_timestamp(invocation.observation.snapshot.observed_at)).total_seconds()
            if age < 0 or age > self._settings.decision_ttl_seconds:
                return finish(OutcomeStatus.REJECTED, reason="observation_stale")
        if isinstance(invocation.observation, IncidentRecord):
            observation = invocation.observation.observation
            if observation.state is not IncidentState.SAFE_HOLD:
                return finish(OutcomeStatus.REJECTED, reason="incident_not_in_safe_hold")
            if self._now() > _parse_timestamp(observation.expires_at):
                return finish(OutcomeStatus.REJECTED, reason="incident_expired")
        try:
            response = self._provider.evaluate(request)
            if hashlib.sha256(_canonical(request.to_mapping())).hexdigest() != digest:
                raise DecisionError("provider mutated its request")
            if not isinstance(response, DecisionResponse):
                raise DecisionError("provider returned an invalid decision response")
            response = _validate_response({"model": response.model, "answers": response.answers,
                                           "usage": response.usage}, request.questions)
            recommendation = _recommendation(response, invocation.role)
        except DecisionError as error:
            return finish(OutcomeStatus.PROVIDER_ERROR, reason=str(error))
        except RecursionError:
            return finish(
                OutcomeStatus.PROVIDER_ERROR,
                reason="provider response exceeded the nesting limit",
            )
        if isinstance(invocation.observation, IncidentRecord):
            expires_at = _parse_timestamp(
                invocation.observation.observation.expires_at
            )
            if self._now() > expires_at:
                return finish(
                    OutcomeStatus.REJECTED,
                    reason="incident_expired",
                    response=response,
                    recommendation=recommendation,
                )
        if self._monotonic() > invocation.expires_at_monotonic:
            return finish(OutcomeStatus.REJECTED, reason="decision_expired",
                          response=response, recommendation=recommendation)
        active_fence = current_fence()
        if active_fence != invocation.source_fence:
            return finish(OutcomeStatus.REJECTED, reason="state_fence_changed",
                          response=response, recommendation=recommendation)
        if recommendation not in invocation.allowed_responses:
            return finish(OutcomeStatus.REJECTED, reason="unauthorized_recommendation",
                          response=response, recommendation=recommendation)
        if not replay and isinstance(invocation.observation, PlayerFrame):
            age = (self._now() - _parse_timestamp(invocation.observation.snapshot.observed_at)).total_seconds()
            if age < 0 or age > self._settings.decision_ttl_seconds:
                return finish(OutcomeStatus.REJECTED, reason="observation_stale",
                              response=response, recommendation=recommendation)
        return finish(OutcomeStatus.RECOMMENDED, response=response, recommendation=recommendation)


@dataclass(frozen=True, slots=True)
class AcceptanceExpectation:
    recommendations: tuple[str, ...]
    target_id: str | None = None

    @classmethod
    def from_mapping(
        cls, observation_id: str, value: Mapping[str, Any]
    ) -> "AcceptanceExpectation":
        value = _mapping(value, f"expectations.{observation_id}")
        _fields(
            value,
            f"expectations.{observation_id}",
            {"recommendations"},
            {"target_id"},
        )
        recommendations = tuple(
            _text(item, "expected recommendation", 64)
            for item in _sequence(
                value["recommendations"], "expected recommendations"
            )
        )
        if (
            not recommendations
            or len(recommendations) > 16
            or len(set(recommendations)) != len(recommendations)
        ):
            raise ValidationError(
                "expected recommendations must contain 1 to 16 unique values"
            )
        target = value.get("target_id")
        return cls(
            recommendations=recommendations,
            target_id=None
            if target is None
            else _text(str(target), "expected target_id", 64),
        )


def load_acceptance_expectations(
    path: Path,
) -> dict[str, AcceptanceExpectation]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=_unique_json_object)
        value = _mapping(value, "acceptance manifest")
        _fields(value, "acceptance manifest", {"version", "expectations"})
        if value["version"] != 1:
            raise ValidationError("acceptance manifest version must be 1")
        raw = _mapping(value["expectations"], "expectations")
        if not raw or len(raw) > 1_000:
            raise ValidationError(
                "expectations must contain 1 to 1000 observations"
            )
        result: dict[str, AcceptanceExpectation] = {}
        for raw_id, expectation in raw.items():
            observation_id = _text(raw_id, "observation_id", 512)
            result[observation_id] = AcceptanceExpectation.from_mapping(
                observation_id, expectation
            )
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise ValueError(f"{path}: {error}") from error


def evaluate_provider_acceptance(
    observations: Sequence[PlayerFrame | IncidentRecord],
    expectations: Mapping[str, AcceptanceExpectation],
    provider: DecisionProvider,
    settings: DecisionSettings,
    *,
    warmups: int = 1,
    repeats: int = 3,
    confidence_threshold: float = 0.8,
    audit_log: Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    request_format: str = "compact",
) -> dict[str, Any]:
    """Measure a provider against immutable synthetic expectations.

    Only aggregate metrics are returned. Provider answers are not assembled into
    a comparison dataset or written as training material.
    """
    if isinstance(warmups, bool) or not isinstance(warmups, int) or not 0 <= warmups <= 20:
        raise ValidationError("warmups must be an integer between 0 and 20")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 100:
        raise ValidationError("repeats must be an integer between 1 and 100")
    if (
        isinstance(confidence_threshold, bool)
        or not isinstance(confidence_threshold, (int, float))
        or not math.isfinite(confidence_threshold)
        or not 0 <= confidence_threshold <= 1
    ):
        raise ValidationError("confidence_threshold must be between zero and one")
    if not observations or len(observations) > 1_000:
        raise ValidationError("acceptance requires 1 to 1000 observations")
    first = observations[0]
    role = (
        DecisionRole.PLAYER
        if isinstance(first, PlayerFrame)
        else DecisionRole.SUPERVISOR
    )
    observation_ids = [item.observation_id for item in observations]
    if len(set(observation_ids)) != len(observation_ids):
        raise ValidationError("acceptance fixture has duplicate observation ids")
    if set(observation_ids) != set(expectations):
        missing = sorted(set(observation_ids) - set(expectations))
        extra = sorted(set(expectations) - set(observation_ids))
        detail = f"missing expectation for {missing[0]}" if missing else f"unused expectation for {extra[0]}"
        raise ValidationError(detail)
    for observation in observations:
        if role is DecisionRole.PLAYER and not isinstance(observation, PlayerFrame):
            raise ValidationError("acceptance fixture cannot mix player and supervisor observations")
        if role is DecisionRole.SUPERVISOR and not isinstance(observation, IncidentRecord):
            raise ValidationError("acceptance fixture cannot mix player and supervisor observations")
        expected = expectations[observation.observation_id]
        unknown = set(expected.recommendations) - set(observation.allowed_responses)
        if unknown:
            raise ValidationError(
                f"expectation uses unadvertised response {sorted(unknown)[0]!r}"
            )
        if expected.target_id is not None:
            if not isinstance(observation, PlayerFrame):
                raise ValidationError("supervisor expectation cannot specify target_id")
            if expected.target_id not in {_target_id(item) for item in observation.targets}:
                raise ValidationError("expected target_id is not present in the player frame")

    toolbox = DecisionToolbox(
        provider,
        settings,
        monotonic=monotonic,
        now=now,
        audit_log=audit_log,
        request_format=request_format,
    )

    def invoke(observation: PlayerFrame | IncidentRecord) -> DecisionOutcome:
        invocation = (
            DecisionInvocation.player(observation, settings, monotonic=monotonic)
            if isinstance(observation, PlayerFrame)
            else DecisionInvocation.supervisor(observation, settings, monotonic=monotonic)
        )
        return toolbox.evaluate_replay(invocation)

    cold_latency: float | None = None
    warmup_nonrecommended = 0
    models: set[str] = set()
    for _ in range(warmups):
        for observation in observations:
            outcome = invoke(observation)
            if cold_latency is None:
                cold_latency = outcome.latency_ms
            if outcome.model is not None:
                models.add(outcome.model)
            if outcome.status is not OutcomeStatus.RECOMMENDED:
                warmup_nonrecommended += 1

    exact = acceptable = target_matches = target_total = 0
    high_confidence_wrong = low_confidence = 0
    provider_errors = rejected = unsupported = 0
    latencies: list[float] = []
    request_sizes: list[int] = []
    for _ in range(repeats):
        for observation in observations:
            outcome = invoke(observation)
            if cold_latency is None:
                cold_latency = outcome.latency_ms
            latencies.append(outcome.latency_ms)
            request_sizes.append(outcome.request_bytes)
            if outcome.model is not None:
                models.add(outcome.model)
            expected = expectations[observation.observation_id]
            recommendation_acceptable = (
                outcome.recommendation in expected.recommendations
            )
            is_exact = (
                outcome.status is OutcomeStatus.RECOMMENDED
                and outcome.recommendation == expected.recommendations[0]
            )
            is_acceptable = (
                outcome.status is OutcomeStatus.RECOMMENDED
                and recommendation_acceptable
            )
            exact += int(is_exact)
            acceptable += int(is_acceptable)
            if expected.target_id is not None:
                target_total += 1
                target_matches += int(
                    outcome.status is OutcomeStatus.RECOMMENDED
                    and outcome.selected_target_id == expected.target_id
                )
            if outcome.confidence is not None:
                if outcome.confidence < confidence_threshold:
                    low_confidence += 1
                if not recommendation_acceptable and outcome.confidence >= confidence_threshold:
                    high_confidence_wrong += 1
            provider_errors += int(outcome.status is OutcomeStatus.PROVIDER_ERROR)
            rejected += int(outcome.status is OutcomeStatus.REJECTED)
            unsupported += int(outcome.status is OutcomeStatus.UNSUPPORTED)

    total = len(observations) * repeats
    return {
        "status": "complete",
        "request_format": request_format,
        "request_bytes_mean": round(sum(request_sizes) / len(request_sizes), 1),
        "request_bytes_max": max(request_sizes),
        "role": role.value,
        "execution": ExecutionPosture.SHADOW.value,
        "observations": len(observations),
        "warmup_repeats": warmups,
        "warmup_evaluations": len(observations) * warmups,
        "warmup_nonrecommended": warmup_nonrecommended,
        "measured_repeats": repeats,
        "measured_evaluations": total,
        "exact_recommendations": exact,
        "acceptable_recommendations": acceptable,
        "exact_recommendation_accuracy": round(exact / total, 6),
        "acceptable_recommendation_accuracy": round(acceptable / total, 6),
        "target_expectations": target_total,
        "target_matches": target_matches,
        "target_accuracy": None if not target_total else round(target_matches / target_total, 6),
        "confidence_threshold": float(confidence_threshold),
        "high_confidence_wrong": high_confidence_wrong,
        "low_confidence": low_confidence,
        "provider_errors": provider_errors,
        "rejected": rejected,
        "unsupported": unsupported,
        "cold_latency_ms": cold_latency,
        "measured_latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
        "models_seen": sorted(models),
        "executed": False,
    }


def load_player_frames(path: Path) -> list[PlayerFrame]:
    return _load_jsonl(path, PlayerFrame.from_mapping, "player frames")


def load_incidents(path: Path) -> list[IncidentRecord]:
    observations = _load_jsonl(path, IncidentObservation.from_mapping, "incidents")
    records: dict[str, IncidentRecord] = {}
    order: list[str] = []
    for observation in observations:
        key = observation.deduplication_key
        if key not in records:
            records[key] = IncidentRecord.detected(observation)
            order.append(key)
        else:
            records[key] = records[key].update_duplicate(observation)
    return [records[key] for key in order]


def _load_jsonl(path: Path, loader: Callable[[Mapping[str, Any]], Any], label: str) -> list[Any]:
    records: list[Any] = []
    with path.open("rb") as stream:
        for line_number, raw in enumerate(iter(lambda: stream.readline(MAX_OBSERVATION_BYTES + 1), b""), 1):
            if line_number > 10_000:
                raise ValueError(f"{path}: fixture exceeds 10000 lines")
            if not raw.strip():
                continue
            if len(raw) > MAX_OBSERVATION_BYTES:
                raise ValueError(f"{path}:{line_number}: observation exceeds {MAX_OBSERVATION_BYTES} bytes")
            try:
                records.append(loader(_mapping(json.loads(raw, object_pairs_hook=_unique_json_object), label)))
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
                raise ValueError(f"{path}:{line_number}: {error}") from error
    if not records:
        raise ValueError(f"{path}: no {label} found")
    return records


def append_decision_audit(path: Path, record: Mapping[str, Any]) -> None:
    """Append one bounded JSONL record, serialized within this process.

    The record is deliberately issued as one ``O_APPEND`` write. A short write
    is reported as an I/O failure instead of retrying and risking interleaving.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ConfigurationError("decision audit path must be a regular file")
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(descriptor, 0o600)
        payload = json.dumps({"recorded_at": datetime.now(timezone.utc).isoformat(), **record, "executed": False},
                             separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        if len(payload) > MAX_AUDIT_RECORD_BYTES:
            raise ConfigurationError("decision audit record exceeds the size limit")
        with _AUDIT_LOCK:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("short write while appending decision audit")
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def decision_check(settings: DecisionSettings, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environment is None else environment
    present = bool(settings.credential_env and env.get(settings.credential_env, "").strip())
    credential_detail = "not configured" if settings.credential_env is None else ("present" if present else "missing")
    audit_status, audit_detail = _audit_readiness(settings)
    credential_ready = settings.credential_env is None or present
    return {
        "name": "decision_provider",
        "status": "ok" if not settings.enabled or (credential_ready and audit_status == "ok") else "warning",
        "detail": "decision provider disabled" if not settings.enabled else
                  f"provider enabled; credential {credential_detail}; audit {audit_detail}",
        "enabled": settings.enabled, "kind": settings.kind.value, "model": settings.model,
        "player_interval_seconds": settings.player_interval_seconds,
        "live_execution": "unsupported", "audit_status": audit_status,
        "audit_detail": audit_detail,
    }


def _audit_readiness(settings: DecisionSettings) -> tuple[str, str]:
    if not settings.enabled:
        return "ok", "not required while disabled"
    path = settings.audit_log
    if path.is_symlink():
        return "warning", f"target is not a regular file: {path}"
    if path.is_file():
        if os.access(path, os.W_OK):
            return "ok", f"writable file {path}"
        return "warning", f"file is not writable: {path}"
    if path.exists():
        return "warning", f"target is not a file: {path}"
    parent = path.parent
    if parent.is_dir() and os.access(parent, os.W_OK):
        return "ok", f"will be created under writable directory {parent}"
    return "warning", f"parent directory is not writable: {parent}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--request-format", choices=("compact", "baseline"), default="compact")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="inspect provider configuration without a provider call")
    player = commands.add_parser("player-shadow-replay",
                                 help="evaluate sanitized player frames without actions")
    supervisor = commands.add_parser("supervisor-shadow-replay",
                                     help="evaluate sanitized event-driven incidents without actions")
    acceptance = commands.add_parser(
        "provider-acceptance",
        help="measure System One against synthetic immutable expectations",
    )
    acceptance.add_argument("role", choices=[item.value for item in DecisionRole])
    acceptance.add_argument("fixture", type=Path)
    acceptance.add_argument("expectations", type=Path)
    acceptance.add_argument("--warmups", type=int, default=1)
    acceptance.add_argument("--repeats", type=int, default=3)
    acceptance.add_argument("--confidence-threshold", type=float, default=0.8)
    acceptance.add_argument("--audit-output", type=Path)
    for replay in (player, supervisor):
        replay.add_argument("fixture", type=Path)
        replay.add_argument("--output", type=Path)
        replay.add_argument("--max-observations", type=int, default=1)
    args = parser.parse_args(argv)
    settings = Settings.load(path=args.config)
    if args.command == "doctor":
        print(json.dumps(decision_check(settings.decisions), indent=2, sort_keys=True))
        return
    if not settings.decisions.enabled:
        raise SystemExit("decisions.enabled must be true for provider evaluation")
    if args.command == "provider-acceptance":
        observations = (
            load_player_frames(args.fixture)
            if args.role == DecisionRole.PLAYER.value
            else load_incidents(args.fixture)
        )
        try:
            report = evaluate_provider_acceptance(
                observations,
                load_acceptance_expectations(args.expectations),
                SystemOneDecisionProvider(settings.decisions),
                settings.decisions,
                warmups=args.warmups,
                repeats=args.repeats,
                confidence_threshold=args.confidence_threshold,
                audit_log=args.audit_output,
                request_format=args.request_format,
            )
        except (ValidationError, ValueError) as error:
            raise SystemExit(str(error)) from error
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    if not 1 <= args.max_observations <= 1000:
        raise SystemExit("--max-observations must be between 1 and 1000")
    provider = SystemOneDecisionProvider(settings.decisions)
    output = args.output or settings.decisions.audit_log
    toolbox = DecisionToolbox(provider, settings.decisions, audit_log=output,
                              request_format=args.request_format)
    if args.command == "supervisor-shadow-replay":
        role = DecisionRole.SUPERVISOR
        observations = load_incidents(args.fixture)[:args.max_observations]
        invocations = [DecisionInvocation.supervisor(item, settings.decisions) for item in observations]
    else:
        role = DecisionRole.PLAYER
        observations = load_player_frames(args.fixture)[:args.max_observations]
        invocations = [DecisionInvocation.player(item, settings.decisions) for item in observations]
    outcomes = [toolbox.evaluate_replay(item) for item in invocations]
    print(json.dumps({
        "status": "complete", "role": role.value, "execution": "shadow",
        "observations": len(outcomes),
        "recommended": sum(item.status is OutcomeStatus.RECOMMENDED for item in outcomes),
        "rejected": sum(item.status is OutcomeStatus.REJECTED for item in outcomes),
        "provider_errors": sum(item.status is OutcomeStatus.PROVIDER_ERROR for item in outcomes),
        "unsupported": sum(item.status is OutcomeStatus.UNSUPPORTED for item in outcomes),
        "output": str(output), "executed": False,
    }, indent=2, sort_keys=True))


def _validate_response(value: Any, questions: Mapping[str, Mapping[str, Any]]) -> DecisionResponse:
    if not isinstance(value, Mapping):
        raise DecisionError("provider response must be an object")
    model, answers, usage = value.get("model"), value.get("answers"), value.get("usage")
    if not isinstance(model, str) or not model.strip():
        raise DecisionError("provider response is missing model")
    if not isinstance(answers, Mapping) or set(answers) != set(questions):
        raise DecisionError("provider response answer ids do not match request")
    if not isinstance(usage, Mapping):
        raise DecisionError("provider response is missing usage")
    clean_usage = {key: _nonnegative(usage.get(key), f"usage.{key}")
                   for key in ("input_tokens", "output_tokens")}
    if "cost" in usage:
        clean_usage["cost"] = _nonnegative_number(usage["cost"], "usage.cost")
    clean: dict[str, Mapping[str, Any]] = {}
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, Mapping) or answer.get("type") != question.get("type"):
            raise DecisionError(f"answer {name} has the wrong type")
        kind = answer["type"]
        if kind == "noul":
            clean[name] = {"type": kind, "noul": _probability(answer.get("noul"), name)}
        elif kind == "choice":
            options = set(_mapping(question.get("criteria"), f"questions.{name}.criteria"))
            choice = answer.get("choice")
            if choice not in options:
                raise DecisionError(f"answer {name} chose an unknown option")
            probabilities = _probabilities(
                answer.get("probabilities"), options, name
            )
            confidence = _probability(
                answer.get("confidence"), f"{name}.confidence"
            )
            highest_probability = max(probabilities.values())
            if (
                probabilities[choice] < highest_probability
                and not math.isclose(
                    probabilities[choice], highest_probability,
                    abs_tol=WIRE_ROUNDING_TOLERANCE,
                )
            ):
                raise DecisionError(
                    f"answer {name} choice is not a highest-probability option"
                )
            clean[name] = {"type": kind, "choice": choice,
                           "probabilities": probabilities,
                           "confidence": confidence}
        elif kind == "score":
            criteria = _sequence(question.get("criteria"), f"questions.{name}.criteria")
            options = {str(index) for index in range(len(criteria))}
            score = answer.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise DecisionError(f"answer {name} has an invalid score")
            clean_score = float(score)
            if not 0 <= clean_score <= len(criteria) - 1:
                raise DecisionError(f"answer {name} score is outside its scale")
            probabilities = _probabilities(
                answer.get("probabilities"), options, name
            )
            expected_score = sum(
                int(option) * probability
                for option, probability in probabilities.items()
            )
            if not math.isclose(
                clean_score, expected_score,
                abs_tol=WIRE_ROUNDING_TOLERANCE,
            ):
                raise DecisionError(
                    f"answer {name} score does not match its probabilities"
                )
            clean[name] = {"type": kind, "score": clean_score,
                           "legend": {str(index): item for index, item in enumerate(criteria)},
                           "probabilities": probabilities,
                           "confidence": _probability(answer.get("confidence"), f"{name}.confidence")}
        else:
            raise DecisionError(f"answer {name} has an unsupported type")
    return DecisionResponse(model.strip(), clean, clean_usage)


def _primary(response: DecisionResponse, role: DecisionRole) -> Mapping[str, Any]:
    return response.answers["next" if role is DecisionRole.PLAYER else "response"]


def _recommendation(response: DecisionResponse, role: DecisionRole) -> str:
    return str(_primary(response, role)["choice"])


def _selected_target(response: DecisionResponse, targets: Sequence[Mapping[str, Any]]) -> str | None:
    answer = response.answers.get("target_priority")
    return str(answer["choice"]) if answer is not None else (_target_id(targets[0]) if len(targets) == 1 else None)


def _probabilities(value: Any, options: set[str], label: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != options:
        raise DecisionError(f"answer {label} probabilities do not match its options")
    result = {str(key): _probability(item, f"{label}.{key}") for key, item in value.items()}
    if not math.isclose(sum(result.values()), 1.0, abs_tol=0.02):
        raise DecisionError(f"answer {label} probabilities do not sum to one")
    return result


def _probability(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionError(f"{label} must be a probability")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise DecisionError(f"{label} must be between zero and one")
    return result


def _nonnegative(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DecisionError(f"{label} must be a nonnegative integer")
    return value


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionError(f"{label} must be a nonnegative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise DecisionError(f"{label} must be a nonnegative number")
    return result


def _target_id(target: Mapping[str, Any]) -> str:
    value = target.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValidationError("each target requires a nonblank string or integer id")
    return str(value).strip()


def _objects(value: Any, label: str, maximum: int) -> tuple[Mapping[str, Any], ...]:
    values = _sequence(value, label)
    if len(values) > maximum:
        raise ValidationError(f"{label} must contain at most {maximum} entries")
    return tuple(_bounded_mapping(item, f"{label}[{index}]") for index, item in enumerate(values))


def _bounded_mapping(value: Any, label: str) -> Mapping[str, Any]:
    return _bounded_json(_mapping(value, label), label)


def _bounded_json(value: Any, label: str, depth: int = 0) -> Any:
    if depth > 5:
        raise ValidationError(f"{label} is nested too deeply")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 2**63 - 1: raise ValidationError(f"{label} integer is out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value): raise ValidationError(f"{label} number must be finite")
        return value
    if isinstance(value, str):
        if len(value) > 4000: raise ValidationError(f"{label} string is too long")
        return value.replace("\x00", "")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > 256: raise ValidationError(f"{label} has too many items")
        return [_bounded_json(item, f"{label}[{index}]", depth + 1) for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        if len(value) > 64: raise ValidationError(f"{label} has too many fields")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValidationError(f"{label} keys must be bounded nonblank strings")
            result[key] = _bounded_json(item, f"{label}.{key}", depth + 1)
        return result
    raise ValidationError(f"{label} must contain only JSON values")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping): raise ValidationError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{label} must be an array")
    return value


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str): raise ValidationError(f"{label} must be a string")
    result = value.replace("\x00", "").strip()
    if not result or len(result) > maximum: raise ValidationError(f"{label} must be 1 to {maximum} characters")
    return result


def _integer(value: Any, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{label} must be an integer >= {minimum}")
    return value


def _timestamp(value: Any) -> str:
    result = _text(value, "timestamp", 64)
    _parse_timestamp(result)
    return result


def _parse_timestamp(value: str) -> datetime:
    try: result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error: raise ValidationError("timestamp must be ISO-8601") from error
    if result.tzinfo is None or result.utcoffset() is None: raise ValidationError("timestamp must include a timezone")
    return result


def _enum(kind: type[StrEnum], value: Any, label: str) -> Any:
    try: return kind(value)
    except (TypeError, ValueError) as error: raise ValidationError(f"unknown {label} {value!r}") from error


def _fields(value: Mapping[str, Any], label: str, required: set[str], optional: set[str] | None = None) -> None:
    optional = set() if optional is None else optional
    unknown, missing = sorted(set(value) - required - optional), sorted(required - set(value))
    if unknown: raise ValidationError(f"unknown {label} field {unknown[0]}")
    if missing: raise ValidationError(f"missing {label} field {missing[0]}")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 3)


if __name__ == "__main__":
    main()
