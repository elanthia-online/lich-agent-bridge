"""Synthetic registered-suite admission, stop, and outcome tests."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from lich_agent_bridge.actions import ActionApproval, ActionBroker, ActionContext, ActionControl, ActionProposal, ActionResult, CommandPolicy
from lich_agent_bridge.controller_manifest import ControllerManifest
from lich_agent_bridge.errors import ConfigurationError, ValidationError
from lich_agent_bridge.operations import CapabilityRunner
from lich_agent_bridge.session_hub import SessionHub
from .test_operations import FakeClock, FakeEvidence, FakeState


REVISION = "a" * 64


def registration():
    return {
        "name": "test-probe", "script": "lab-test-runner", "summary": "Synthetic lifecycle probe",
        "characters": ["Testmage"], "result_global": "$lab_test_result",
        "signal_global": "$lab_test_cancel", "lanes": ["movement", "combat"],
        "owner_scripts": ["lab-test-runner", "lab-synthetic-probe"],
        "safe_handoff": {"kind": "room", "room_id": "1234"}, "capability_action": "start",
        "actions": [{"name": "start", "kind": "launch", "launch_mode": "start",
            "command_template": "lab-test probe {revision} {case_id}",
            "script_args_template": "probe {revision} {case_id}",
            "policy": {"category": "configuration", "confirmation_required": True},
            "parameters": [{"name": "revision", "type": "enum", "values": [REVISION]},
                           {"name": "case_id", "type": "enum", "values": ["basic", "all"]}]}],
        "test_suite": {"manifest": "suites/probe.json", "files": {
            "suites/probe.json": REVISION, "lab-test-runner.lic": "b" * 64,
            "lab-test-runner.rb": "c" * 64, "lab-synthetic-probe.lic": "d" * 64}},
    }


def manifest(raw=None):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "controllers.json"
        path.write_text(json.dumps({"version": 1, "controllers": [raw or registration()]}))
        return ControllerManifest.load(path)


class PilotManifestTests(unittest.TestCase):
    def test_fixed_revision_pinned_registration_loads(self):
        controller = manifest().controller("test-probe")
        self.assertEqual(controller.test_suite, registration()["test_suite"])

    def test_invalid_pin_paths_and_digests_rejected(self):
        for path, digest in (("../probe.lic", "e" * 64), ("/tmp/probe.lic", "e" * 64),
                             ("sub\\probe.lic", "e" * 64), ("probe.lic", "no-digest")):
            with self.subTest(path=path):
                raw = registration()
                raw["test_suite"]["files"][path] = digest
                with self.assertRaises(ConfigurationError):
                    manifest(raw)

    def test_test_metadata_cannot_authorize_another_runner_or_command(self):
        for field, value in (("script", "arbitrary-script"), ("characters", ["Testmage", "Other"]),
                             ("safe_handoff", {"kind": "owners_released"})):
            with self.subTest(field=field):
                raw = registration()
                raw[field] = value
                with self.assertRaises(ConfigurationError):
                    manifest(raw)
        raw = registration()
        raw["actions"][0]["parameters"][0]["values"] = ["e" * 64]
        with self.assertRaises(ConfigurationError):
            manifest(raw)


class PilotBackendTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.registry = manifest()
        self.broker = ActionBroker(policy=CommandPolicy(self.registry), clock=self.clock)
        self.broker.admit_generation("Testmage", "generation-1")
        self.broker.control(ActionControl(character="Testmage", enabled=True))
        self.state = FakeState()
        self.state.fresh, self.state.sequence = True, 1
        self.state.dead, self.state.stunned = False, False
        self.state.scripts = ()
        self.state.owners = {"movement": None, "combat": None}
        self.evidence = FakeEvidence()
        self.details = {"suite_id": "probe", "revision": REVISION, "case_id": "basic",
                        "status": "passed", "assertions_passed": True, "cleanup_complete": True,
                        "report_path": "/private/synthetic-report.json"}
        self.evidence.controller_result = {"ok": True, "code": "passed", "message": "Probe passed",
                                           "details": self.details}
        self.actions = []
        self.runner = CapabilityRunner(actions=self.broker, state=self.state, evidence=self.evidence,
                                       controller_manifest=self.registry, clock=self.clock,
                                       sleeper=self.clock.sleep, step_hook=self.dispatch)

    def dispatch(self, broker, action, snapshot):
        self.actions.append(action["action_id"])
        context = ActionContext(character="Testmage", room_id="1234", generation="generation-1")
        offered = broker.poll(context)
        broker.approve(ActionApproval(action_id=offered["action_id"], character="Testmage",
                                     room_id="1234", generation="generation-1"))
        broker.poll(context)
        broker.record_result(ActionResult(action_id=action["action_id"], character="Testmage",
                                          outcome="completed", detail="started", generation="generation-1"))
        self.state.sequence += 1

    def run_case(self, **kwargs):
        return self.runner.perform("Testmage", "controller.test-probe",
                                   {"revision": REVISION, "case_id": "basic"},
                                   expected_generation=kwargs.pop("expected_generation", "generation-1"), **kwargs)

    def test_success_requires_matching_pins_assertions_and_clean_handoff(self):
        self.assertEqual(self.run_case().status, "succeeded")

    def test_test_admission_requires_generation_and_registered_room(self):
        self.assertEqual(self.run_case(expected_generation=None).status, "failed")
        self.state.room_id = "9999"
        self.assertEqual(self.run_case().status, "failed")
        self.assertEqual(self.actions, [])

    def test_mismatched_revision_or_incomplete_assertions_fail_with_evidence(self):
        for field, value in (("revision", "f" * 64), ("case_id", "all"), ("assertions_passed", False),
                             ("cleanup_complete", False), ("status", "inconclusive")):
            with self.subTest(field=field):
                self.setUp()
                self.details[field] = value
                result = self.run_case()
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.evidence[0].facts["details"][field], value)

    def test_alive_and_exact_owner_script_exit_required_after_run(self):
        for field, value in (("dead", True), ("scripts", ("lab-synthetic-probe",)),
                             ("room_id", "9999")):
            with self.subTest(field=field):
                self.setUp()
                original = self.dispatch
                def finish(*args):
                    original(*args)
                    setattr(self.state, field, value)
                self.runner._step_hook = finish
                self.assertEqual(self.run_case().status, "failed")

    def test_stop_after_dispatch_preserves_sent_status_and_terminal_report(self):
        original = self.dispatch
        def stop(*args):
            original(*args)
            self.runner.interrupt(self.runner.history()[-1].operation_id)
        self.runner._step_hook = stop
        result = self.run_case()
        action = self.broker.get(self.actions[0])
        self.assertTrue(action["stop_requested"])
        self.assertEqual(action["status"], "completed")
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(len(result.evidence), 1)

    def test_stop_pending_launch_revokes_exact_approval(self):
        def stop(broker, action, snapshot):
            self.actions.append(action["action_id"])
            self.runner.interrupt(self.runner.history()[-1].operation_id)
        self.runner._step_hook = stop
        result = self.run_case()
        action = self.broker.get(self.actions[0])
        self.assertEqual(action["status"], "cancelled")
        self.assertEqual(result.status, "interrupted")

    def test_stop_during_submit_revokes_launch_before_driver_sees_it(self):
        submit = self.broker.submit
        def stop_before_return(proposal):
            result = submit(proposal)
            self.actions.append(result["action_id"])
            self.runner.interrupt(self.runner.history()[-1].operation_id)
            return result
        self.broker.submit = stop_before_return
        self.runner._step_hook = Mock(side_effect=AssertionError("cancelled launch reached driver"))
        result = self.run_case()
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(self.broker.get(self.actions[0])["status"], "cancelled")
        self.runner._step_hook.assert_not_called()

    def test_stop_while_waiting_preserves_failed_terminal_cleanup_report(self):
        entered, released = threading.Event(), threading.Event()
        verify = self.evidence.verify_controller
        def wait_for_cleanup(*args):
            entered.set()
            self.assertTrue(released.wait(2))
            return verify(*args)
        self.evidence.verify_controller = wait_for_cleanup
        results = []
        worker = threading.Thread(target=lambda: results.append(self.run_case()))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            operation = self.runner.history()[-1]
            self.runner.interrupt(operation.operation_id)
            self.assertTrue(self.broker.get(self.actions[0])["stop_requested"])
            self.assertEqual(operation.status, "running")
            self.details["status"] = "failed"
            self.details["assertions_passed"] = False
            self.evidence.controller_result["ok"] = False
        finally:
            released.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].status, "interrupted")
        self.assertEqual(results[0].evidence[0].facts["details"]["status"], "failed")

    def test_actions_off_or_generation_replacement_requests_local_stop(self):
        for replace_generation in (False, True):
            with self.subTest(replace_generation=replace_generation):
                self.setUp()
                original = self.dispatch
                def revoke(*args):
                    original(*args)
                    if replace_generation:
                        self.broker.admit_generation("Testmage", "generation-2")
                    else:
                        self.broker.control(ActionControl(character="Testmage", enabled=False))
                self.runner._step_hook = revoke
                result = self.run_case()
                self.assertTrue(self.broker.get(self.actions[0])["stop_requested"])
                self.assertNotEqual(result.status, "succeeded")

    def test_broker_stop_refuses_wrong_owner_or_non_test_action(self):
        action = self.broker.submit(ActionProposal(character="Testmage", command="info"))
        with self.assertRaises(ValidationError):
            self.broker.request_test_stop(action["action_id"], character="Testmage", generation="generation-1")
        self.assertEqual(self.broker.get(action["action_id"])["status"], "queued")
        action = self.broker.submit(ActionProposal(character="Testmage", command=f"lab-test probe {REVISION} basic"))
        for character, generation in (("Other", "generation-1"), ("Testmage", "generation-2")):
            with self.assertRaises(ValidationError):
                self.broker.request_test_stop(action["action_id"], character=character, generation=generation)
        self.assertEqual(self.broker.get(action["action_id"])["status"], "confirmation_required")

    def test_hub_exact_stop_cannot_interrupt_a_successor(self):
        hub = SessionHub(world_state=Mock(), actions=self.broker, inventory=Mock(), knowledge=Mock(),
                         capabilities=self.runner)
        first = self.run_case()
        successor = self.runner._create_operation("Testmage", "controller.test-probe",
                                                  {"revision": REVISION, "case_id": "basic"},
                                                  timeout_seconds=30, expected_generation="generation-1")
        stopped = hub.stop_operation({"character": "Testmage", "operation_id": first.operation_id,
                                      "expected_generation": "generation-1"})
        self.assertFalse(stopped["stopped"])
        self.assertNotIn(successor.operation_id, self.runner._interruptions)
        with self.assertRaises(ValidationError):
            hub.stop_operation({"character": "Testmage"})
        with self.assertRaises(ValidationError):
            hub.stop_operation({"character": "Testmage", "operation_id": successor.operation_id,
                                "expected_generation": "generation-2"})
        stopped = hub.stop_operation({"character": "Testmage", "operation_id": successor.operation_id,
                                      "expected_generation": "generation-1"})
        self.assertTrue(stopped["stop_requested"])
        self.assertIn(successor.operation_id, self.runner._interruptions)

    def test_hub_forwards_admission_generation_and_rejects_malformed_value(self):
        hub = SessionHub(world_state=Mock(), actions=self.broker, inventory=Mock(), knowledge=Mock(),
                         capabilities=self.runner)
        payload = {"character": "Testmage", "capability": "controller.test-probe",
                   "args": {"revision": REVISION, "case_id": "basic"},
                   "expected_generation": "generation-1"}
        self.assertEqual(hub.perform(payload)["status"], "succeeded")
        payload["expected_generation"] = "generation-2"
        self.assertEqual(hub.perform(payload)["status"], "failed")
        for invalid in (None, False, ""):
            payload["expected_generation"] = invalid
            with self.assertRaises(ValidationError):
                hub.perform(payload)

    def test_launch_expiry_never_extends_fractional_operation_deadline(self):
        register = self.evidence.register_controller
        def delayed_registration(*args):
            self.clock.sleep(0.25)
            return register(*args)
        self.evidence.register_controller = delayed_registration
        result = self.run_case()
        self.assertEqual(result.status, "succeeded")
        self.assertLessEqual(self.broker.get(self.actions[0])["expires_at"], result.deadline)

    def test_approval_delay_consumes_the_same_thirty_second_envelope(self):
        dispatch = self.dispatch
        def delayed_approval(*args):
            self.clock.sleep(18)
            dispatch(*args)
        self.runner._step_hook = delayed_approval
        verify = self.evidence.verify_controller
        def cleanup_with_remaining_time(registration, action_id, timeout):
            self.assertLessEqual(timeout, 12)
            self.clock.sleep(11.5)
            return verify(registration, action_id, timeout)
        self.evidence.verify_controller = cleanup_with_remaining_time
        result = self.run_case()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.deadline - result.requested_at, 30)
        self.assertLess(result.ended_at, result.deadline)

    def test_available_terminal_report_is_retained_at_deadline_without_waiting(self):
        dispatch = self.dispatch
        def delayed_ack(*args):
            dispatch(*args)
            self.clock.sleep(30)
        self.runner._step_hook = delayed_ack
        verify = self.evidence.verify_controller
        self.evidence.verify_controller = Mock(side_effect=verify)
        result = self.run_case()
        self.assertEqual(result.status, "timed_out")
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(self.evidence.verify_controller.call_args.args[2], 0)
        self.assertTrue(self.broker.get(self.actions[0])["stop_requested"])

    def test_expired_evidence_wait_is_timeout_and_late_success_is_not_success(self):
        for available in (False, True):
            with self.subTest(available=available):
                self.setUp()
                verify = self.evidence.verify_controller
                def deadline(registration, action_id, timeout):
                    self.clock.sleep(timeout)
                    return verify(registration, action_id, 0) if available else None
                self.evidence.verify_controller = deadline
                result = self.run_case()
                self.assertEqual(result.status, "timed_out")
                self.assertEqual(len(result.evidence), int(available))

    def test_failed_launch_ack_retains_available_attributed_report(self):
        record_result = self.broker.record_result
        def report_failure(result):
            return record_result(ActionResult(action_id=result.action_id, character=result.character,
                                              generation=result.generation, outcome="failed", detail="suite failed"))
        self.broker.record_result = report_failure
        self.evidence.controller_result["ok"] = False
        self.details["status"] = "failed"
        result = self.run_case()
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(result.evidence), 1)

    def test_internal_timeout_override_cannot_expand_pilot_envelope(self):
        result = self.run_case(timeout_seconds=31)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.actions, [])


if __name__ == "__main__":
    unittest.main()
