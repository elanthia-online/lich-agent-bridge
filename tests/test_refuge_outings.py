"""Offline synthetic refuge outing contracts: no game connection or commands."""

from copy import deepcopy
import unittest

from lich_agent_bridge.actions import ActionBroker, ActionContext, ActionControl, ActionProposal, CommandPolicy
from lich_agent_bridge.errors import ConfigurationError, ValidationError
from lich_agent_bridge.operations import CapabilityRunner, HandItem
from lich_agent_bridge.session_hub import SessionHub
from lich_agent_bridge.session_hub import WorldStateEvidenceAdapter
from lich_agent_bridge.protocol import MeaningfulEvent
from lich_agent_bridge.world_state import WorldState
from .test_session_hub import publish_snapshot
from unittest.mock import Mock
from .test_operations import BrokerDriver, FakeClock, FakeEvidence, FakeState
from .test_quick_area import area_manifest_raw, load_manifest


def refuge_manifest_raw():
    raw = area_manifest_raw()
    controller = raw["controllers"][0]
    controller["safe_handoff"] = {"kind": "quick_refuge", "room_id": "1000", "return_seconds": 30}
    controller["actions"][0].update(command_template="bigshot quick watch --area profile",
                                      script_args_template="quick watch --area profile")
    return raw


class RefugeOutingTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest(refuge_manifest_raw())
        self.clock, self.state, self.evidence = FakeClock(), FakeState(), FakeEvidence()
        self.state.room_id, self.state.fresh, self.state.sequence = "1000", True, 2
        self.state.dead, self.state.stunned = False, False
        self.state.hands = {"left": None, "right": HandItem("777", "test weapon")}
        self.state.scripts, self.state.owners = (), {"movement": None, "combat": None}
        self.broker = ActionBroker(policy=CommandPolicy(self.manifest), clock=self.clock)
        self.broker.admit_generation("Testmage", "generation-1")
        self.broker.control(ActionControl(character="Testmage", enabled=True))
        self.driver = BrokerDriver(self.broker, self.state)
        self.facts = {"ok": True, "code": "quick_completed", "message": "Synthetic refuge result",
                      "details": {"cleanup_complete": True, "runtime": {
                          "state": "completed", "work_result": {"state": "completed", "reason": "room_clear"},
                          "refuge": {"room_id": 1000, "phase": "finished", "returned": True, "equipment_restored": True}}}}
        self.verify_hook = None
        self.evidence.controller_result = self.facts
        original_verify = self.evidence.verify_controller

        def verify(registration, action_id, timeout_seconds):
            self.facts["details"]["run_id"] = action_id
            self.state.sequence += 1
            if self.verify_hook:
                self.verify_hook(registration[0], action_id)
            return original_verify(registration, action_id, timeout_seconds)

        self.evidence.verify_controller = verify
        self.runner = CapabilityRunner(actions=self.broker, state=self.state, evidence=self.evidence,
                                       controller_manifest=self.manifest, clock=self.clock,
                                       sleeper=self.clock.sleep, step_hook=self.driver, history_limit=1)

    def run_outing(self, **overrides):
        kwargs = {"expected_generation": "generation-1", "timeout_seconds": 90}
        kwargs.update(overrides)
        return self.runner.perform("Testmage", "controller.quick", **kwargs)

    def test_complete_outing_requires_terminal_proof_and_fresh_original_equipment(self):
        result = self.run_outing()
        self.assertEqual(result.status, "succeeded", result.explanation)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.alerts, [])
        self.assertFalse(self.runner._refuge_pending)

    def test_generic_controller_refuge_uses_the_same_verified_handoff(self):
        raw = refuge_manifest_raw()
        raw["controllers"][0]["safe_handoff"]["kind"] = "controller_refuge"
        manifest = load_manifest(raw)
        broker = ActionBroker(policy=CommandPolicy(manifest), clock=self.clock)
        broker.admit_generation("Testmage", "generation-1")
        broker.control(ActionControl(character="Testmage", enabled=True))
        driver = BrokerDriver(broker, self.state)
        runner = CapabilityRunner(actions=broker, state=self.state, evidence=self.evidence,
                                  controller_manifest=manifest, clock=self.clock,
                                  sleeper=self.clock.sleep, step_hook=driver, history_limit=1)

        result = runner.perform("Testmage", "controller.quick",
                                expected_generation="generation-1", timeout_seconds=90)

        self.assertEqual(result.status, "succeeded", result.explanation)
        self.assertIn("Controlled outing completed", result.explanation)
        self.assertFalse(runner._refuge_pending)

    def test_hub_forwards_explicit_outing_budget_for_sync_and_async_calls(self):
        hub = SessionHub(world_state=Mock(), actions=self.broker, inventory=Mock(), knowledge=Mock(), capabilities=self.runner)
        payload = {"character": "Testmage", "capability": "controller.quick",
                   "expected_generation": "generation-1", "timeout_seconds": 90}
        self.assertEqual(hub.perform(payload)["status"], "succeeded")
        started = hub.start_operation(payload)
        self.runner.wait(started["operation_id"], timeout_seconds=1)
        self.assertEqual(self.runner.get(started["operation_id"]).status, "succeeded")

    def test_hub_rejects_malformed_or_excessive_budget_before_admission(self):
        hub = SessionHub(world_state=Mock(), actions=self.broker, inventory=Mock(), knowledge=Mock(), capabilities=self.runner)
        for timeout in (None, True, 0, -1, 301, float("inf"), float("nan"), "90"):
            with self.subTest(timeout=timeout), self.assertRaises(ValidationError):
                hub.start_operation({"character": "Testmage", "capability": "controller.quick", "timeout_seconds": timeout})
        self.assertEqual(self.driver.commands, [])

    def test_larger_budget_does_not_expand_unrelated_capabilities(self):
        result = self.runner.perform("Testmage", "room.loot", timeout_seconds=90)
        self.assertEqual(result.status, "failed")
        self.assertIn("30-second", result.explanation)
        self.assertEqual(self.driver.commands, [])

    def test_removing_controls_cannot_bypass_refuge_requirement_for_quick(self):
        raw = refuge_manifest_raw()
        controller = raw["controllers"][0]
        controller.pop("control_owner_scripts")
        controller["safe_handoff"] = {"kind": "room", "room_id": "1000"}
        controller["actions"] = controller["actions"][:1]
        manifest = load_manifest(raw)
        runner = CapabilityRunner(actions=self.broker, state=self.state, evidence=self.evidence,
                                   controller_manifest=manifest, clock=self.clock, sleeper=self.clock.sleep,
                                   step_hook=self.driver)
        result = runner.perform("Testmage", "controller.quick", expected_generation="generation-1")
        self.assertEqual(result.status, "failed")
        self.assertIn("quick_refuge", result.explanation)
        self.assertEqual(self.driver.commands, [])

    def test_admission_rejects_unknown_generation_bad_window_and_non_refuge_start(self):
        for kwargs in ({"expected_generation": None}, {"timeout_seconds": 42}, {"timeout_seconds": 301}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.run_outing(**kwargs).status, "failed")
        self.state.room_id = "1001"
        self.assertIn("begin", self.run_outing().explanation)
        self.assertEqual(self.driver.commands, [])

    def test_unknown_hands_or_conflicting_scripts_cannot_depart(self):
        for hands in (None, {}, {"left": None}, {"left": None, "right": HandItem("", "unknown")}):
            self.state.hands = hands
            self.assertEqual(self.run_outing().status, "failed")
        self.state.hands = {"left": None, "right": None}
        for scripts in (None, ("bigshot",), ("go2",)):
            self.state.scripts = scripts
            self.assertEqual(self.run_outing().status, "failed")
        self.assertEqual(self.driver.commands, [])

    def test_failed_work_retains_evidence_but_safe_return_does_not_latch(self):
        self.facts["ok"], self.facts["code"] = False, "ineffective_action"
        self.facts["details"]["runtime"]["work_result"]["state"] = "stopped"
        result = self.run_outing()
        self.assertEqual(result.status, "failed")
        self.assertIn("safe return verified", result.explanation)
        self.assertEqual(len(result.evidence), 1)
        self.assertFalse(result.alerts)
        self.assertFalse(self.runner._refuge_pending)

    def test_verified_native_preflight_rejection_does_not_claim_an_unsafe_handoff(self):
        self.driver.fail_for.add("bigshot quick watch --area profile")
        self.facts = {
            "ok": False,
            "code": "controlled_start_rejected",
            "message": "native preflight rejected before child launch",
            "details": {
                "run_id": "set by verify hook",
                "child_started": False,
                "cleanup_complete": True,
                "effects_verified": False,
            },
        }
        self.evidence.controller_result = self.facts

        result = self.run_outing()

        self.assertEqual(result.status, "failed")
        self.assertIn("before child launch", result.explanation)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.alerts, [])
        self.assertFalse(self.runner._refuge_pending)

    def test_success_claim_cannot_hide_stopped_or_unknown_work(self):
        for state in ("stopped", None, "running"):
            self.facts["details"]["runtime"]["work_result"]["state"] = state
            result = self.run_outing()
            self.assertEqual(result.status, "failed")
            self.assertIn("safe return verified", result.explanation)
            self.assertFalse(result.alerts)

    def test_stop_before_dispatch_cancels_instead_of_issuing_return_commands(self):
        def predispatch(broker, proposal, snapshot):
            operation_id = self.evidence.registrations[-1][0]
            self.runner.interrupt(operation_id)
            self.assertEqual(broker.get(proposal["action_id"])["status"], "cancelled")
            self.assertIsNone(broker.poll(ActionContext(character="Testmage", room_id="1000")))
        self.runner._step_hook = predispatch
        result = self.run_outing()
        self.assertNotEqual(result.status, "succeeded")
        self.assertEqual(self.driver.commands, [])

    def test_missing_result_latches_and_fresh_refuge_resolution_is_required(self):
        self.evidence.controller_result = None
        result = self.run_outing()
        self.assertTrue(result.alerts)
        self.state.room_id = "1001"
        # Terminal history eviction must not lose the unsafe-outing exclusion.
        second = self.run_outing()
        self.assertEqual(second.status, "failed")
        self.assertEqual(len(self.driver.commands), 1)
        self.state.room_id = "1000"
        self.evidence.controller_result = self.facts
        self.assertEqual(self.run_outing().status, "succeeded")

    def test_new_session_cannot_silently_erase_an_unsafe_outing(self):
        self.evidence.controller_result = None
        self.run_outing()
        self.state.generation = "generation-2"
        self.broker.admit_generation("Testmage", "generation-2")
        result = self.run_outing(expected_generation="generation-2")
        self.assertIn("previous session", result.explanation)
        self.assertEqual(len(self.driver.commands), 1)

    def test_explicit_exact_native_recovery_receipt_allows_new_session_admission(self):
        self.evidence.controller_result = None
        failed = self.run_outing()
        _, controller, original, action_id = self.runner._refuge_pending["testmage"]
        self.state.generation = "generation-2"
        self.state.sequence = 1  # a new generation starts its own counter
        self.broker.admit_generation("Testmage", "generation-2")
        world = WorldState()
        publish_snapshot(world, generation="generation-2")
        self.evidence.verify_controller_recovery = WorldStateEvidenceAdapter(world).verify_controller_recovery
        receipt = {"controller": controller.name, "action_id": action_id,
                   "previous_generation": original.generation, "operator_confirmed": True,
                   "room_id": "1000", "hands": {"left": None, "right": "777"}}

        def publish(data, kind="controller_recovery"):
            world.publish_event(MeaningfulEvent.from_mapping({
                "character": "Testmage", "generation": "generation-2",
                "observed_at": "2026-08-31T12:00:02Z", "kind": kind,
                "summary": "Synthetic operator recovery", "data": data}))

        for field, wrong in (("controller", "other"), ("action_id", "other-action"),
                             ("previous_generation", "other-generation"), ("operator_confirmed", False),
                             ("room_id", "1001"), ("hands", {"left": None, "right": "999"})):
            publish({**receipt, field: wrong})
            result = self.run_outing(expected_generation="generation-2")
            self.assertIn("previous session", result.explanation, field)
            self.assertEqual(len(self.driver.commands), 1)
        publish(receipt, kind="controller_result")
        self.assertIn("previous session", self.run_outing(expected_generation="generation-2").explanation)
        publish(receipt)
        self.state.hands = {"left": HandItem("888", "knife"), "right": HandItem("777", "test weapon")}
        self.assertIn("equipment", self.run_outing(expected_generation="generation-2").explanation)
        self.assertEqual(len(self.driver.commands), 1)
        self.state.hands = {"left": None, "right": HandItem("777", "test weapon")}
        self.evidence.controller_result = self.facts
        result = self.run_outing(expected_generation="generation-2")
        self.assertEqual(result.status, "succeeded", result.explanation)
        self.assertFalse(self.runner._refuge_pending)
        self.assertEqual(failed.status, "failed")
        self.assertTrue(failed.alerts)

    def test_typed_capability_recovers_exact_pending_handoff_in_owning_session(self):
        self.evidence.controller_result = None
        failed = self.run_outing()
        _, _controller, _original, action_id = self.runner._refuge_pending["testmage"]
        self.state.generation = "generation-2"
        self.state.sequence = 1
        self.broker.admit_generation("Testmage", "generation-2")
        self.evidence.controller_recovery_receipt_result = {
            "controller": "quick",
            "action_id": action_id,
            "previous_generation": "generation-1",
            "operator_confirmed": True,
            "room_id": "1000",
            "hands": {"left": None, "right": "777"},
        }

        result = self.runner.perform(
            "Testmage",
            "session.recover_controller",
            {"action_id": action_id, "confirm": True},
            expected_generation="generation-2",
        )

        self.assertEqual(result.status, "succeeded", result.explanation)
        self.assertEqual(self.driver.commands[-1], f"lab recover {action_id} confirm")
        self.assertFalse(self.runner._refuge_pending)
        self.assertEqual(failed.status, "failed")
        self.assertTrue(failed.alerts)

    def test_typed_recovery_uses_native_receipt_after_sidecar_restart(self):
        self.evidence.controller_result = None
        self.run_outing()
        _, _controller, _original, action_id = self.runner._refuge_pending["testmage"]
        restarted = CapabilityRunner(
            actions=self.broker,
            state=self.state,
            evidence=self.evidence,
            controller_manifest=self.manifest,
            clock=self.clock,
            sleeper=self.clock.sleep,
            step_hook=self.driver,
        )
        self.state.generation = "generation-2"
        self.state.sequence = 1
        self.broker.admit_generation("Testmage", "generation-2")
        self.evidence.controller_recovery_receipt_result = {
            "controller": "quick",
            "action_id": action_id,
            "previous_generation": "generation-1",
            "operator_confirmed": True,
            "room_id": "1000",
            "hands": {"left": None, "right": "777"},
        }

        result = restarted.perform(
            "Testmage",
            "session.recover_controller",
            {"action_id": action_id, "confirm": True},
            expected_generation="generation-2",
        )

        self.assertEqual(result.status, "succeeded", result.explanation)
        self.assertEqual(self.driver.commands[-1], f"lab recover {action_id} confirm")
        self.assertFalse(restarted._refuge_pending)

    def test_typed_recovery_refuses_wrong_id_or_missing_explicit_confirmation(self):
        self.evidence.controller_result = None
        self.run_outing()
        command_count = len(self.driver.commands)
        cases = (
            ({"action_id": "ffffffffffffffff", "confirm": True}, "does not match"),
            ({"action_id": "0123456789abcdef", "confirm": False}, "confirm=true"),
            ({"action_id": "0123456789abcdef"}, "only action_id and confirm"),
        )
        for arguments, detail in cases:
            with self.subTest(arguments=arguments):
                result = self.runner.perform(
                    "Testmage",
                    "session.recover_controller",
                    arguments,
                    expected_generation="generation-1",
                )
                self.assertEqual(result.status, "failed")
                self.assertIn(detail, result.explanation)
        self.assertEqual(len(self.driver.commands), command_count)
        self.assertTrue(self.runner._refuge_pending)

    def test_typed_recovery_keeps_exclusion_without_matching_native_evidence(self):
        self.evidence.controller_result = None
        self.run_outing()
        _, _controller, _original, action_id = self.runner._refuge_pending["testmage"]
        self.state.generation = "generation-2"
        self.state.sequence = 1
        self.broker.admit_generation("Testmage", "generation-2")
        self.evidence.controller_recovery_receipt_result = None

        result = self.runner.perform(
            "Testmage",
            "session.recover_controller",
            {"action_id": action_id, "confirm": True},
            expected_generation="generation-2",
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("matching controller recovery evidence", result.explanation)
        self.assertTrue(self.runner._refuge_pending)

    def test_reaching_refuge_with_wrong_hands_is_not_safe(self):
        self.verify_hook = lambda *_: setattr(self.state, "hands", {"left": HandItem("888", "knife"), "right": HandItem("777", "test weapon")})
        result = self.run_outing()
        self.assertEqual(result.status, "failed")
        self.assertIn("not restored", result.explanation)
        self.assertTrue(result.alerts)

    def test_return_proof_is_strict_even_when_controller_claims_success(self):
        original = deepcopy(self.facts)
        mutations = (
            lambda d: d.update(run_id="wrong"),
            lambda d: d.update(cleanup_complete=False),
            lambda d: d["runtime"].update(state="running"),
            lambda d: d["runtime"].pop("work_result"),
            lambda d: d["runtime"]["refuge"].update(room_id="1000"),
            lambda d: d["runtime"]["refuge"].update(phase="returning"),
            lambda d: d["runtime"]["refuge"].update(returned="true"),
            lambda d: d["runtime"]["refuge"].update(equipment_restored=False),
        )
        for mutation in mutations:
            self.facts.clear()
            self.facts.update(deepcopy(original))
            self.verify_hook = lambda *_: mutation(self.facts["details"])
            with self.subTest(mutation=mutation):
                result = self.run_outing()
                self.assertEqual(result.status, "failed")
                self.assertTrue(result.alerts)

    def test_stop_preserves_return_authority_and_waits_for_safe_result(self):
        def stop(operation_id, action_id):
            self.runner.interrupt(operation_id)
            action = self.broker.get(action_id)
            self.assertTrue(action["return_requested"])
            self.assertFalse(action["stop_requested"])
            self.assertEqual(self.runner.get(operation_id).status, "running")
            self.runner.interrupt(operation_id)  # idempotent
        self.verify_hook = stop
        result = self.run_outing()
        self.assertEqual(result.status, "interrupted", result.explanation)
        self.assertIn("verified", result.explanation)
        self.assertFalse(result.alerts)

    def test_actions_off_is_hard_revocation_not_return_permission(self):
        def revoke(operation_id, action_id):
            self.broker.control(ActionControl(character="Testmage", enabled=False))
            with self.assertRaises(ValidationError):
                self.broker.request_controller_return(action_id, character="Testmage", generation="generation-1")
            self.runner.interrupt(operation_id)
            self.assertTrue(self.broker.get(action_id)["stop_requested"])
            self.assertFalse(self.broker.get(action_id)["return_requested"])
        self.verify_hook = revoke
        result = self.run_outing()
        self.assertEqual(result.status, "interrupted")

    def test_return_request_requires_exact_authorized_refuge_launch(self):
        def requests(_, action_id):
            for character, generation in (("Other", "generation-1"), ("Testmage", "wrong")):
                with self.assertRaises(ValidationError):
                    self.broker.request_controller_return(action_id, character=character, generation=generation)
            self.clock.now += 91
            with self.assertRaises(ValidationError):
                self.broker.request_controller_return(action_id, character="Testmage", generation="generation-1")
        self.verify_hook = requests
        self.assertEqual(self.run_outing().status, "timed_out")
        ordinary = self.broker.submit(ActionProposal(character="Testmage", command="look"))
        with self.assertRaises(ValidationError):
            self.broker.request_controller_return(ordinary["action_id"], character="Testmage", generation="generation-1")


class RefugeSchemaTests(unittest.TestCase):
    def test_refuge_requires_fixed_positive_non_sentinel_room_and_bounded_return_reserve(self):
        for field, values in (("room_id", (None, True, 0, 4, "04", " 1000", "1000.0", "1" * 13)),
                              ("return_seconds", (None, True, 9, 121, 30.0, "30"))):
            for value in values:
                raw = refuge_manifest_raw()
                raw["controllers"][0]["safe_handoff"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ConfigurationError):
                    load_manifest(raw)

    def test_reserve_and_profile_area_cannot_be_smuggled_into_other_handoffs(self):
        for mutation in (lambda c: c["safe_handoff"].update(kind="room"),
                         lambda c: c["safe_handoff"].update(extra=True),
                         lambda c: c.update(lanes=["combat"]),
                         lambda c: c["actions"][0].update(script_args_template="quick clear")):
            raw = refuge_manifest_raw()
            mutation(raw["controllers"][0])
            with self.assertRaises(ConfigurationError):
                load_manifest(raw)
