from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import lich_agent_bridge.decisions as decisions_module
from lich_agent_bridge.decisions import (
    AcceptanceExpectation,
    DecisionError,
    DecisionFence,
    DecisionInvocation,
    DecisionRequest,
    DecisionResponse,
    DecisionRole,
    DecisionToolbox,
    ExecutionPosture,
    IncidentObservation,
    IncidentRecord,
    NoRedirectHandler,
    OutcomeStatus,
    PlayerFrame,
    SystemOneDecisionProvider,
    append_decision_audit,
    decision_check,
    evaluate_provider_acceptance,
    load_acceptance_expectations,
    load_incidents,
    load_player_frames,
)
from lich_agent_bridge.errors import ConfigurationError, ValidationError
from lich_agent_bridge.settings import Settings


def frame_mapping(*, sequence: int = 12, targets: list[dict] | None = None) -> dict:
    return {
        "snapshot": {
            "character": "Testmage", "generation": "generation-1", "sequence": sequence,
            "observed_at": "2026-09-21T00:00:00+00:00",
            "room": {"id": "3946", "title": "Synthetic refuge"},
            "vitals": {"health": {"current": 100, "max": 100}},
            "roundtime": 0, "stunned": False, "dead": False,
        },
        "plan": {"id": "synthetic-phase", "revision": 3, "step": "engage",
                 "authorized": ["attack_target", "buff_ally", "wait", "retreat"]},
        "targets": targets if targets is not None else [{"id": "target-1", "noun": "training target", "held": True}],
        "party": [{"name": "Testfriend", "health": "fine"}],
        "recent": [{"kind": "synthetic_miss", "age": 0.2}],
        "cooldowns": {"profile_buff": "ready"},
        "last_op": {"kind": "profile_action", "status": "succeeded"},
    }


def incident_mapping(*, sequence: int = 40, observed_at: str = "2026-09-21T00:00:00+00:00",
                     state: str = "safe_hold", response: str | None = None) -> dict:
    result = {
        "deduplication_key": "controller-a:stalled:synthetic-phase",
        "kind": "controller_stalled", "severity": "danger", "state": state,
        "source_fence": {
            "generation": "generation-1", "sequence": sequence, "room_id": "3946",
            "target_ids": [], "plan_id": "synthetic-phase", "plan_revision": 3,
        },
        "affected_characters": ["Testmage", "Testfriend"],
        "allowed_responses": ["hold", "return_field", "abort", "request_human"],
        "observed_at": observed_at, "expires_at": "2030-09-21T00:00:30+00:00",
        "facts": {"controller": "synthetic-controller", "progress_age_seconds": 12},
    }
    if response is not None:
        result["allowed_responses"] = [response]
    return result


def incident_record(*, state: str = "safe_hold") -> IncidentRecord:
    detected = IncidentObservation.from_mapping(incident_mapping(
        sequence=39, observed_at="2026-09-20T23:59:59+00:00", state="detected"
    ))
    record = IncidentRecord.detected(detected)
    if state == "detected":
        return record
    return record.update_duplicate(IncidentObservation.from_mapping(incident_mapping(state=state)))


def response_for(request: DecisionRequest, recommendation: str | None = None) -> DecisionResponse:
    answers = {}
    for name, question in request.questions.items():
        kind = question["type"]
        if kind == "choice":
            options = list(question["criteria"])
            choice = recommendation if recommendation in options else options[0]
            probabilities = {option: float(option == choice) for option in options}
            answers[name] = {"type": "choice", "choice": choice,
                             "probabilities": probabilities, "confidence": 1.0}
        elif kind == "score":
            answers[name] = {"type": "score", "score": 1.0,
                             "legend": {"0": "idle", "1": "routine", "2": "pressing", "3": "emergency"},
                             "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0},
                             "confidence": 1.0}
        else:
            answers[name] = {"type": "noul", "noul": 0.1}
    return DecisionResponse(
        "system-one-test",
        answers,
        {"input_tokens": 100, "output_tokens": 20, "cost": 0.0042},
    )


class FakeProvider:
    def __init__(self, recommendation: str | None = None, error: str | None = None):
        self.recommendation = recommendation
        self.error = error
        self.requests: list[DecisionRequest] = []

    def evaluate(self, request: DecisionRequest) -> DecisionResponse:
        self.requests.append(request)
        if self.error:
            raise DecisionError(self.error)
        return response_for(request, self.recommendation)


class Clock:
    def __init__(self, *values: float):
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


class DateClock:
    def __init__(self, *values: datetime):
        self.values = iter(values)

    def __call__(self) -> datetime:
        return next(self.values)


class ManualClock:
    def __init__(self, value: float = 10.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance_ms(self, milliseconds: float) -> None:
        self.value += milliseconds / 1000


class AcceptanceProvider:
    def __init__(self, clock: ManualClock, outcomes: list[dict]):
        self.clock = clock
        self.outcomes = iter(outcomes)

    def evaluate(self, request: DecisionRequest) -> DecisionResponse:
        spec = next(self.outcomes)
        self.clock.advance_ms(spec.get("latency_ms", 0))
        if spec.get("error"):
            raise DecisionError(spec["error"])
        response = response_for(request, spec["recommendation"])
        answers = {key: dict(value) for key, value in response.answers.items()}
        primary_name = "next" if "next" in answers else "response"
        primary = dict(answers[primary_name])
        confidence = spec.get("confidence", 1.0)
        options = list(primary["probabilities"])
        choice = primary["choice"]
        remainder = (1.0 - confidence) / max(1, len(options) - 1)
        primary["probabilities"] = {
            option: confidence if option == choice else remainder
            for option in options
        }
        primary["confidence"] = confidence
        answers[primary_name] = primary
        if "target_priority" in answers and spec.get("target_id") is not None:
            target = dict(answers["target_priority"])
            target["choice"] = spec["target_id"]
            target["probabilities"] = {
                option: float(option == spec["target_id"])
                for option in target["probabilities"]
            }
            target["confidence"] = 1.0
            answers["target_priority"] = target
        return DecisionResponse(
            spec.get("model", "acceptance-model"), answers, response.usage
        )


class DecisionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings.load(
            self.root / "config.toml", environment={"HOME": str(self.root)},
            overrides={"decisions": {"enabled": True, "credential_env": None}},
        ).decisions

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def toolbox(self, provider: FakeProvider, **kwargs) -> DecisionToolbox:
        return DecisionToolbox(provider, self.settings, audit_log=self.root / "audit.jsonl", **kwargs)

    def test_provider_settings_are_role_free_and_do_not_retain_secrets(self) -> None:
        settings = Settings.load(self.root / "default.toml", environment={
            "HOME": str(self.root), "JEV_API_KEY": "never-store-me",
        })
        serialized = settings.redacted()
        self.assertFalse(settings.decisions.enabled)
        self.assertEqual(settings.decisions.player_interval_seconds, 1.0)
        self.assertNotIn("role", serialized["decisions"])
        self.assertNotIn("execution", serialized["decisions"])
        self.assertNotIn("never-store-me", json.dumps(serialized))

    def test_player_frame_builds_bounded_questions_and_exact_fence(self) -> None:
        frame = PlayerFrame.from_mapping(frame_mapping())
        request = frame.request(self.settings.model)
        self.assertEqual(request.state["role"], "player")
        self.assertEqual(request.state["exact"]["target_count"], 1)
        self.assertFalse(request.state["exact"]["roundtime_blocking"])
        self.assertNotIn("target_priority", request.questions)
        self.assertEqual(frame.fence.target_ids, ("target-1",))

    def test_player_shadow_uses_common_recommended_outcome_and_audit(self) -> None:
        provider = FakeProvider("attack_target")
        frame = PlayerFrame.from_mapping(frame_mapping())
        invocation = DecisionInvocation.player(frame, self.settings, monotonic=lambda: 10.0)
        outcome = self.toolbox(
            provider, monotonic=Clock(10.0, 10.1, 10.2)
        ).evaluate_replay(invocation)
        self.assertIs(outcome.status, OutcomeStatus.RECOMMENDED)
        self.assertIs(outcome.role, DecisionRole.PLAYER)
        self.assertIs(outcome.execution, ExecutionPosture.SHADOW)
        self.assertEqual(outcome.recommendation, "attack_target")
        self.assertEqual(outcome.selected_target_id, "target-1")
        self.assertFalse(outcome.executed)
        audit = json.loads((self.root / "audit.jsonl").read_text())
        self.assertEqual(audit["status"], "recommended")
        self.assertEqual(audit["usage"]["cost"], 0.0042)
        self.assertFalse(audit["executed"])

    def test_live_is_unsupported_before_provider_evaluation(self) -> None:
        provider = FakeProvider()
        frame = PlayerFrame.from_mapping(frame_mapping())
        invocation = DecisionInvocation.player(frame, self.settings, execution=ExecutionPosture.LIVE,
                                               monotonic=lambda: 10.0)
        outcome = self.toolbox(
            provider, monotonic=Clock(10.0, 10.0)
        ).evaluate_replay(invocation)
        self.assertIs(outcome.status, OutcomeStatus.UNSUPPORTED)
        self.assertEqual(outcome.reason, "live_execution_not_installed")
        self.assertEqual(provider.requests, [])

    def test_runtime_evaluation_requires_a_current_fence_supplier(self) -> None:
        frame = PlayerFrame.from_mapping(frame_mapping())
        invocation = DecisionInvocation.player(
            frame, self.settings, monotonic=lambda: 10.0
        )
        with self.assertRaises(TypeError):
            self.toolbox(FakeProvider()).evaluate(invocation)  # type: ignore[call-arg]

    def test_every_fence_dimension_rejects_an_inflight_player_answer(self) -> None:
        frame = PlayerFrame.from_mapping(frame_mapping())
        changes = {
            "generation": "generation-2", "sequence": frame.fence.sequence + 1,
            "room_id": "other-room", "target_ids": ("other-target",),
            "plan_id": "other-plan", "plan_revision": frame.fence.plan_revision + 1,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                provider = FakeProvider()
                invocation = DecisionInvocation.player(frame, self.settings, monotonic=lambda: 10.0)
                fences = iter([frame.fence, replace(frame.fence, **{field: value})])
                outcome = self.toolbox(provider, monotonic=Clock(10.0, 10.1, 10.2),
                                       now=lambda: datetime(2026, 9, 21, tzinfo=timezone.utc)).evaluate(
                    invocation, current_fence=lambda: next(fences))
                self.assertEqual(len(provider.requests), 1)
                self.assertIs(outcome.status, OutcomeStatus.REJECTED)
                self.assertEqual(outcome.reason, "state_fence_changed")

    def test_expired_invocation_and_provider_failure_are_nonexecuting(self) -> None:
        frame = PlayerFrame.from_mapping(frame_mapping())
        expired = DecisionInvocation.player(frame, self.settings, monotonic=lambda: 5.0)
        provider = FakeProvider()
        result = self.toolbox(
            provider, monotonic=Clock(10.0, 10.0)
        ).evaluate_replay(expired)
        self.assertEqual(result.reason, "decision_expired")
        self.assertEqual(provider.requests, [])
        failed_provider = FakeProvider(error="offline")
        current = DecisionInvocation.player(frame, self.settings, monotonic=lambda: 20.0)
        result = self.toolbox(
            failed_provider, monotonic=Clock(20.0, 20.0)
        ).evaluate_replay(current)
        self.assertIs(result.status, OutcomeStatus.PROVIDER_ERROR)
        self.assertFalse(result.executed)

    def test_supervisor_is_event_driven_and_uses_bounded_vocabulary(self) -> None:
        incident = incident_record()
        provider = FakeProvider("return_field")
        invocation = DecisionInvocation.supervisor(incident, self.settings, monotonic=lambda: 10.0)
        outcome = self.toolbox(provider, monotonic=Clock(10.0, 10.1, 10.2),
                               now=lambda: datetime(2026, 9, 21, tzinfo=timezone.utc)).evaluate_replay(invocation)
        self.assertIs(outcome.status, OutcomeStatus.RECOMMENDED)
        self.assertEqual(outcome.role.value, "supervisor")
        self.assertEqual(outcome.recommendation, "return_field")
        self.assertEqual(set(provider.requests[0].questions), {"response"})
        self.assertNotIn("interval", provider.requests[0].state)

    def test_incident_requires_local_safe_hold_before_provider_call(self) -> None:
        incident = incident_record(state="detected")
        provider = FakeProvider()
        invocation = DecisionInvocation.supervisor(incident, self.settings, monotonic=lambda: 10.0)
        outcome = self.toolbox(provider, monotonic=Clock(10.0, 10.0),
                               now=lambda: datetime(2026, 9, 21, tzinfo=timezone.utc)).evaluate_replay(invocation)
        self.assertEqual(outcome.reason, "incident_not_in_safe_hold")
        self.assertEqual(provider.requests, [])

    def test_duplicate_observations_keep_id_advance_version_and_evaluate_once(self) -> None:
        path = self.root / "incidents.jsonl"
        first = incident_mapping(sequence=39, observed_at="2026-09-20T23:59:59+00:00", state="detected")
        second = incident_mapping(sequence=41, observed_at="2026-09-21T00:00:01+00:00")
        path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
        incidents = load_incidents(path)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].version, 2)
        self.assertTrue(incidents[0].incident_id.startswith("inc-"))
        self.assertEqual(incidents[0].fence.sequence, 41)
        self.assertEqual(incidents[0].fence.incident_version, 2)
        provider = FakeProvider("hold")
        invocation = DecisionInvocation.supervisor(incidents[0], self.settings, monotonic=lambda: 10.0)
        self.toolbox(provider, monotonic=Clock(10.0, 10.1, 10.2),
                     now=lambda: datetime(2026, 9, 21, tzinfo=timezone.utc)).evaluate_replay(invocation)
        self.assertEqual(len(provider.requests), 1)

    def test_duplicate_incident_preserves_source_identity_and_advances_sequence(self) -> None:
        incident = incident_record()
        baseline = incident_mapping(
            sequence=41, observed_at="2026-09-21T00:00:01+00:00"
        )
        for field, value in (
            ("generation", "generation-2"),
            ("plan_id", "other-plan"),
            ("plan_revision", 4),
        ):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(baseline))
                changed["source_fence"][field] = value
                with self.assertRaisesRegex(
                    ValidationError, "source identity changed"
                ):
                    incident.update_duplicate(
                        IncidentObservation.from_mapping(changed)
                    )
        stale = json.loads(json.dumps(baseline))
        stale["source_fence"]["sequence"] = incident.fence.sequence
        with self.assertRaisesRegex(ValidationError, "sequence must increase"):
            incident.update_duplicate(IncidentObservation.from_mapping(stale))

    def test_incident_expiry_is_rechecked_after_provider_response(self) -> None:
        incident = incident_record()
        provider = FakeProvider("hold")
        invocation = DecisionInvocation.supervisor(
            incident, self.settings, monotonic=lambda: 10.0
        )
        before = datetime(2026, 9, 21, tzinfo=timezone.utc)
        after = datetime(2031, 9, 21, tzinfo=timezone.utc)
        outcome = self.toolbox(
            provider,
            monotonic=Clock(10.0, 10.1),
            now=DateClock(before, after),
        ).evaluate_replay(invocation)
        self.assertEqual(len(provider.requests), 1)
        self.assertIs(outcome.status, OutcomeStatus.REJECTED)
        self.assertEqual(outcome.reason, "incident_expired")

    def test_changed_incident_version_rejects_inflight_answer(self) -> None:
        incident = incident_record()
        invocation = DecisionInvocation.supervisor(incident, self.settings, monotonic=lambda: 10.0)
        provider = FakeProvider("hold")
        fences = iter([incident.fence, replace(incident.fence, incident_version=incident.version + 1)])
        outcome = self.toolbox(provider, monotonic=Clock(10.0, 10.1, 10.2),
                               now=lambda: datetime(2026, 9, 21, tzinfo=timezone.utc)).evaluate(
            invocation, current_fence=lambda: next(fences))
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(outcome.reason, "state_fence_changed")

    def test_unknown_role_posture_response_and_shapes_fail_closed(self) -> None:
        frame = PlayerFrame.from_mapping(frame_mapping())
        with self.assertRaisesRegex(ValidationError, "unknown decision role"):
            DecisionInvocation("pilot", ExecutionPosture.SHADOW, frame, frame.authorized, frame.fence,
                               10.0, self.settings.kind, self.settings.model)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValidationError, "unknown execution posture"):
            DecisionInvocation(DecisionRole.PLAYER, "maybe", frame, frame.authorized, frame.fence,
                               10.0, self.settings.kind, self.settings.model)  # type: ignore[arg-type]
        bad = incident_mapping(response="raw_command")
        with self.assertRaises(ValidationError):
            IncidentObservation.from_mapping(bad)
        single = incident_mapping()
        single["allowed_responses"] = ["hold"]
        with self.assertRaisesRegex(ValidationError, "at least two"):
            IncidentObservation.from_mapping(single)
        with self.assertRaisesRegex(ValidationError, "unknown player frame field"):
            PlayerFrame.from_mapping({**frame_mapping(), "command": "attack"})

    def test_system_one_bearer_is_optional_and_never_retained(self) -> None:
        frame = PlayerFrame.from_mapping(frame_mapping())
        request = frame.request(self.settings.model)
        response = response_for(request)
        payload = json.dumps({"model": response.model, "answers": response.answers,
                              "usage": response.usage, "provider_extension": True}).encode()
        captured = []
        def opener(wire, *, timeout):
            captured.append((wire, timeout))
            return io.BytesIO(payload)
        local = SystemOneDecisionProvider(self.settings, opener=opener)
        local_result = local.evaluate(request)
        self.assertEqual(
            captured[-1][0].full_url,
            "https://api.typesafe.ai/v1/systemone",
        )
        self.assertEqual(local_result.usage["cost"], 0.0042)
        self.assertIsNone(captured[-1][0].get_header("Authorization"))
        self.assertEqual(captured[-1][0].get_header("User-agent"), "lich-agent-bridge/system-one")
        authenticated_settings = replace(self.settings, credential_env="JEV_API_KEY")
        authenticated = SystemOneDecisionProvider(authenticated_settings,
                                                   environment={"JEV_API_KEY": "private-key"}, opener=opener)
        result = authenticated.evaluate(request)
        self.assertEqual(captured[-1][0].get_header("Authorization"), "Bearer private-key")
        self.assertNotIn("private-key", repr(result))
        with self.assertRaises(ConfigurationError):
            SystemOneDecisionProvider(authenticated_settings, environment={})

    def test_system_one_uses_configured_openrouter_endpoint_and_validates_cost(self) -> None:
        frame = PlayerFrame.from_mapping(frame_mapping())
        request = frame.request(self.settings.model)
        response = response_for(request)
        captured = []

        def opener(wire, *, timeout):
            captured.append(wire)
            return io.BytesIO(json.dumps({
                "model": response.model,
                "answers": response.answers,
                "usage": response.usage,
            }).encode())

        hosted_settings = SimpleNamespace(
            enabled=True,
            kind=self.settings.kind,
            base_url="https://openrouter.ai",
            endpoint_path="/api/alpha/decisions",
            credential_env=None,
            timeout_seconds=self.settings.timeout_seconds,
        )
        provider = SystemOneDecisionProvider(hosted_settings, opener=opener)
        result = provider.evaluate(request)
        self.assertEqual(
            captured[0].full_url,
            "https://openrouter.ai/api/alpha/decisions",
        )
        self.assertEqual(result.usage["cost"], 0.0042)

        for bad_cost in (-0.1, "unknown", True):
            with self.subTest(cost=bad_cost):
                payload = {
                    "model": response.model,
                    "answers": response.answers,
                    "usage": {**response.usage, "cost": bad_cost},
                }
                invalid = SystemOneDecisionProvider(
                    hosted_settings,
                    opener=lambda wire, timeout, payload=payload: io.BytesIO(
                        json.dumps(payload).encode()
                    ),
                )
                with self.assertRaisesRegex(DecisionError, "usage.cost"):
                    invalid.evaluate(request)

    def test_system_one_redirect_handler_never_builds_a_followup_request(self) -> None:
        handler = NoRedirectHandler()
        request = decisions_module.Request(
            "https://127.0.0.1/v1/systemone",
            headers={"Authorization": "Bearer private-key"},
        )
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://attacker.invalid/"
        )
        self.assertIsNone(redirected)

    def test_recursive_provider_response_becomes_provider_error(self) -> None:
        adapter = SystemOneDecisionProvider(
            self.settings, opener=lambda request, timeout: io.BytesIO(b"{}")
        )
        frame = PlayerFrame.from_mapping(frame_mapping())
        with mock.patch.object(
            decisions_module.json, "loads", side_effect=RecursionError
        ):
            with self.assertRaisesRegex(DecisionError, "nesting"):
                adapter.evaluate(frame.request(self.settings.model))

        class RecursiveProvider:
            def evaluate(self, request: DecisionRequest) -> DecisionResponse:
                raise RecursionError

        frame = PlayerFrame.from_mapping(frame_mapping())
        invocation = DecisionInvocation.player(
            frame, self.settings, monotonic=lambda: 10.0
        )
        outcome = DecisionToolbox(
            RecursiveProvider(), self.settings, monotonic=Clock(10.0, 10.0),
            audit_log=self.root / "recursive-audit.jsonl",
        ).evaluate_replay(invocation)
        self.assertIs(outcome.status, OutcomeStatus.PROVIDER_ERROR)
        self.assertIn("nesting", outcome.reason or "")

    def test_choice_argmax_and_score_distribution_are_validated_independently_from_confidence(self) -> None:
        frame = PlayerFrame.from_mapping(frame_mapping())
        request = frame.request(self.settings.model)
        response = response_for(request)

        def evaluates(mutator) -> DecisionResponse:
            payload = {
                "model": response.model,
                "answers": json.loads(json.dumps(response.answers)),
                "usage": response.usage,
            }
            mutator(payload["answers"])
            provider = SystemOneDecisionProvider(
                self.settings,
                opener=lambda request, timeout: io.BytesIO(
                    json.dumps(payload).encode()
                ),
            )
            return provider.evaluate(request)

        def rejects(mutator) -> None:
            with self.assertRaises(DecisionError):
                evaluates(mutator)

        live_shaped = evaluates(lambda answers: answers["next"].update(
            choice="attack_target",
            probabilities={
                "attack_target": 0.82,
                "buff_ally": 0.06,
                "wait": 0.06,
                "retreat": 0.06,
            },
            confidence=0.73,
        ))
        self.assertEqual(live_shaped.answers["next"]["confidence"], 0.73)
        self.assertEqual(
            live_shaped.answers["next"]["probabilities"]["attack_target"],
            0.82,
        )

        rounded_tie = evaluates(lambda answers: answers["next"].update(
            choice="attack_target",
            probabilities={
                "attack_target": 0.33,
                "buff_ally": 0.34,
                "wait": 0.33,
                "retreat": 0.0,
            },
            confidence=0.2,
        ))
        self.assertEqual(rounded_tie.answers["next"]["choice"], "attack_target")

        score_confidence = evaluates(
            lambda answers: answers["urgency"].update(confidence=0.42)
        )
        self.assertEqual(score_confidence.answers["urgency"]["confidence"], 0.42)

        for score, zero_probability, one_probability in (
            (0.62, 0.39, 0.61),
            (0.64, 0.37, 0.63),
        ):
            with self.subTest(score=score):
                rounded_score = evaluates(
                    lambda answers, score=score,
                    zero_probability=zero_probability,
                    one_probability=one_probability: answers["urgency"].update(
                        score=score,
                        probabilities={
                            "0": zero_probability,
                            "1": one_probability,
                            "2": 0.0,
                            "3": 0.0,
                        },
                        confidence=0.73,
                    )
                )
                self.assertEqual(
                    rounded_score.answers["urgency"]["score"], score
                )

        rejects(lambda answers: answers["next"].update(
            probabilities={
                "attack_target": 0.2,
                "buff_ally": 0.7,
                "wait": 0.05,
                "retreat": 0.05,
            },
            confidence=0.73,
        ))
        rejects(lambda answers: answers["next"].update(confidence=1.1))
        rejects(lambda answers: answers["urgency"].update(score=4.0))
        rejects(lambda answers: answers["urgency"].update(score=2.0))
        rejects(lambda answers: answers["urgency"].update(
            score=0.65,
            probabilities={"0": 0.37, "1": 0.63, "2": 0.0, "3": 0.0},
        ))

    def test_provider_acceptance_reports_bounded_quality_and_latency(self) -> None:
        frame = PlayerFrame.from_mapping(frame_mapping(targets=[
            {"id": "target-1", "noun": "training target", "held": True},
            {"id": "target-2", "noun": "training target", "held": False},
        ]))
        clock = ManualClock()
        provider = AcceptanceProvider(clock, [
            {"recommendation": "attack_target", "target_id": "target-1", "latency_ms": 100},
            {"recommendation": "attack_target", "target_id": "target-1", "confidence": 0.9, "latency_ms": 10},
            {"recommendation": "wait", "target_id": "target-2", "confidence": 0.5, "latency_ms": 20},
            {"recommendation": "retreat", "target_id": "target-1", "confidence": 0.95, "latency_ms": 30},
        ])
        report = evaluate_provider_acceptance(
            [frame],
            {frame.observation_id: AcceptanceExpectation(
                ("attack_target", "wait"), "target-1"
            )},
            provider,
            self.settings,
            warmups=1,
            repeats=3,
            confidence_threshold=0.8,
            audit_log=self.root / "acceptance-audit.jsonl",
            monotonic=clock,
        )
        self.assertEqual(report["exact_recommendation_accuracy"], 0.333333)
        self.assertEqual(report["acceptable_recommendation_accuracy"], 0.666667)
        self.assertEqual(report["target_accuracy"], 0.666667)
        self.assertEqual(report["high_confidence_wrong"], 1)
        self.assertEqual(report["low_confidence"], 1)
        self.assertEqual(report["cold_latency_ms"], 100.0)
        self.assertEqual(report["measured_latency_ms"], {"p50": 20.0, "p95": 30.0})
        self.assertEqual(report["models_seen"], ["acceptance-model"])
        self.assertFalse(report["executed"])
        self.assertEqual(
            len((self.root / "acceptance-audit.jsonl").read_text().splitlines()),
            4,
        )

    def test_provider_acceptance_supports_supervisor_and_counts_errors(self) -> None:
        incident = incident_record()
        clock = ManualClock()
        report = evaluate_provider_acceptance(
            [incident],
            {incident.observation_id: AcceptanceExpectation(("hold",))},
            AcceptanceProvider(clock, [{"error": "offline", "latency_ms": 4}]),
            self.settings,
            warmups=0,
            repeats=1,
            audit_log=self.root / "supervisor-acceptance.jsonl",
            monotonic=clock,
            now=lambda: datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        self.assertEqual(report["role"], "supervisor")
        self.assertEqual(report["provider_errors"], 1)
        self.assertEqual(report["rejected"], 0)
        self.assertEqual(report["acceptable_recommendation_accuracy"], 0.0)

    def test_acceptance_expectations_fail_closed_on_duplicates_and_gaps(self) -> None:
        duplicate = self.root / "duplicate-expectations.json"
        duplicate.write_text(
            '{"version":1,"expectations":{"same":{"recommendations":["wait"]},'
            '"same":{"recommendations":["retreat"]}}}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            load_acceptance_expectations(duplicate)

        frame = PlayerFrame.from_mapping(frame_mapping())
        with self.assertRaisesRegex(ValidationError, "missing expectation"):
            evaluate_provider_acceptance(
                [frame], {}, FakeProvider(), self.settings,
                audit_log=self.root / "unused.jsonl",
            )

    def test_shipped_acceptance_manifests_match_their_synthetic_fixtures(self) -> None:
        examples = Path(__file__).resolve().parents[1] / "examples"
        player = load_player_frames(
            examples / "system-one-acceptance-player.example.jsonl"
        )
        player_expected = load_acceptance_expectations(
            examples / "system-one-acceptance-player.expectations.json"
        )
        supervisor = load_incidents(
            examples / "system-one-acceptance-supervisor.example.jsonl"
        )
        supervisor_expected = load_acceptance_expectations(
            examples / "system-one-acceptance-supervisor.expectations.json"
        )
        self.assertEqual({item.observation_id for item in player}, set(player_expected))
        self.assertEqual({item.observation_id for item in supervisor}, set(supervisor_expected))

    def test_audit_is_private_append_only_and_contains_no_execution_path(self) -> None:
        output = self.root / "nested" / "audit.jsonl"
        append_decision_audit(output, {"status": "recommended"})
        append_decision_audit(output, {"status": "rejected"})
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual([row["status"] for row in rows], ["recommended", "rejected"])
        self.assertTrue(all(row["executed"] is False for row in rows))
        toolbox = self.toolbox(FakeProvider())
        self.assertFalse(hasattr(toolbox, "broker"))
        self.assertNotIn("command", json.dumps(rows))

    def test_audit_append_does_not_require_unix_fchmod(self) -> None:
        output = self.root / "portable-audit.jsonl"
        with mock.patch.object(decisions_module.os, "fchmod", None):
            append_decision_audit(output, {"status": "recommended"})
        self.assertFalse(json.loads(output.read_text())["executed"])

    def test_loaders_name_bad_line_and_doctor_uses_provider_only_fields(self) -> None:
        path = self.root / "frames.jsonl"
        path.write_text(json.dumps(frame_mapping()) + "\n{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"frames.jsonl:2"):
            load_player_frames(path)
        report = decision_check(replace(self.settings, credential_env="JEV_API_KEY"),
                                {"JEV_API_KEY": "private-key"})
        self.assertTrue(report["enabled"])
        self.assertEqual(report["player_interval_seconds"], 1.0)
        self.assertEqual(report["live_execution"], "unsupported")
        self.assertNotIn("private-key", repr(report))

    def test_decision_doctor_warns_when_enabled_audit_target_is_invalid(self) -> None:
        target = self.root / "audit-directory"
        target.mkdir()
        report = decision_check(
            replace(self.settings, audit_log=target), environment={}
        )
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["audit_status"], "warning")
        self.assertIn("not a file", report["audit_detail"])


if __name__ == "__main__":
    unittest.main()
