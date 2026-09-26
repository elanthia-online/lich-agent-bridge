"""Synthetic registration and real broker tests; no game session or runtime binding."""

import json
from pathlib import Path
import tempfile
import threading
import unittest

from lich_agent_bridge.actions import (
    ActionApproval, ActionBroker, ActionContext, ActionControl, ActionProposal, ActionResult, CommandPolicy,
)
from lich_agent_bridge.controller_manifest import ControllerManifest
from lich_agent_bridge.errors import ConfigurationError, ValidationError
from lich_agent_bridge.operations import CapabilityRunner
from .test_operations import BrokerDriver, FakeClock, FakeEvidence, FakeState


FIXTURE = Path(__file__).parent / "fixtures" / "controller-controls.json"
RUN_ID = "0123456789abcdef"


class ControllerControlSchemaTests(unittest.TestCase):
    def setUp(self):
        self.manifest = ControllerManifest.load(FIXTURE)

    def test_registered_controls_bind_exact_launch_action_token(self):
        controller = self.manifest.controller("quick")
        for verb in ("status", "hold", "resume", "retreat"):
            with self.subTest(verb=verb):
                command, args, normalized = controller.action(verb).build({"run_id": RUN_ID})
                self.assertEqual(command, f"lab-test-quick {verb} {RUN_ID}")
                self.assertEqual(args, "")
                self.assertEqual(normalized, {"run_id": RUN_ID})
                matched = self.manifest.match_command(command)
                self.assertEqual(matched.action.kind, "control")
                self.assertEqual(matched.arguments, normalized)
        self.assertEqual(controller.capability_action, "start")

    def test_malformed_tokens_and_extra_arguments_fail_closed(self):
        action = self.manifest.controller("quick").action("hold")
        for value in (None, True, 1234567890123456, "", RUN_ID.upper(),
                      RUN_ID + "0", " " + RUN_ID, RUN_ID + ";north"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    action.build({"run_id": value})
                if not isinstance(value, int):
                    self.assertIsNone(self.manifest.match_command(f"lab-test-quick hold {value}"))
        with self.assertRaises(ValidationError):
            action.build({"run_id": RUN_ID, "command": "attack"})
        self.assertIsNone(self.manifest.match_command(f"lab-test-quick stop {RUN_ID}"))

    def test_control_schema_cannot_expand_to_script_execution_or_unpinned_control(self):
        mutations = (
            lambda data: data.update(name="stop"),
            lambda data: data.update(script_args_template="hold"),
            lambda data: data.update(launch_mode="run"),
            lambda data: data.update(parameters=[]),
            lambda data: data["parameters"][0].update(type="numeric"),
            lambda data: data["parameters"][0].update(values=[RUN_ID]),
            lambda data: data["policy"].update(category="inspection"),
            lambda data: data["policy"].update(confirmation_required=False),
        )
        for mutate in mutations:
            raw = json.loads(FIXTURE.read_text())
            mutate(raw["controllers"][0]["actions"][2])
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "controllers.json"
                path.write_text(json.dumps(raw))
                with self.assertRaises(ConfigurationError):
                    ControllerManifest.load(path)

    def test_control_cannot_be_advertised_as_an_independent_capability(self):
        raw = json.loads(FIXTURE.read_text())
        raw["controllers"][0]["capability_action"] = "hold"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controllers.json"
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ConfigurationError, "launch capability"):
                ControllerManifest.load(path)

    def test_public_default_keeps_controls_unregistered(self):
        public = ControllerManifest.load(FIXTURE.parents[2] / "lich" / "lab-controllers.json")
        with self.assertRaises(ValidationError):
            CommandPolicy(public).evaluate(f"lab-test-quick hold {RUN_ID}")

    def test_opt_in_bigshot_example_uses_direct_named_trial_and_numeric_target(self):
        example = ControllerManifest.load(FIXTURE.parents[2] / "examples/controllers/bigshot-quick-trial.json")
        controller = example.controller("quick-trial")
        command, arguments, _ = controller.action("start").build({"sequence": "probe-sequence", "target_id": 12345})
        self.assertEqual(controller.script, "bigshot")
        self.assertEqual(arguments, "quick trial probe-sequence --target 12345 --area profile")
        self.assertEqual(command, "bigshot " + arguments)
        self.assertEqual(controller.control_owner_scripts, ("bigshot",))
        for args in ({"sequence": "unreviewed", "target_id": 12345},
                     {"sequence": "probe-sequence", "target_id": "a troll"}):
            with self.assertRaises(ValidationError):
                controller.action("start").build(args)

    def test_control_owners_must_be_explicit_and_within_exclusion_contract(self):
        for owners in (None, [], ["bigshot"], ["lab-test-quick", "unregistered-script"]):
            raw = json.loads(FIXTURE.read_text())
            controller = raw["controllers"][0]
            if owners is None:
                controller.pop("control_owner_scripts")
            else:
                controller["control_owner_scripts"] = owners
            with self.subTest(owners=owners), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "controllers.json"
                path.write_text(json.dumps(raw))
                with self.assertRaises(ConfigurationError):
                    ControllerManifest.load(path)


class ControllerControlBrokerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.audit = []
        self.broker = ActionBroker(
            policy=CommandPolicy(ControllerManifest.load(FIXTURE)),
            clock=lambda: self.now, audit=self.audit.append,
        )
        self.broker.admit_generation("Testmage", "session-1")
        self.broker.control(ActionControl(
            character="Testmage", generation="session-1", enabled=True,
        ))
        self.context = ActionContext("Testmage", "1000", "session-1")

    def submit(self, verb="hold"):
        action = self.broker.submit(ActionProposal(
            character="Testmage", command=f"lab-test-quick {verb} {RUN_ID}",
            expected_generation="session-1", expected_room_id="1000", ttl_seconds=2,
        ))
        if action["status"] == "confirmation_required":
            self.broker.approve(ActionApproval(
                action_id=action["action_id"], character="Testmage", room_id="1000",
                generation="session-1",
            ))
        return self.broker.get(action["action_id"])

    def test_control_uses_existing_audit_and_dispatch_not_an_execution_bypass(self):
        action = self.submit()
        self.assertEqual(action["status"], "queued")
        dispatched = self.broker.poll(self.context)
        self.assertEqual(dispatched["action_id"], action["action_id"])
        self.assertEqual(dispatched["command"], f"lab-test-quick hold {RUN_ID}")
        self.assertEqual(dispatched["status"], "execute")
        self.assertEqual(self.broker.get(action["action_id"])["status"], "dispatched")
        self.assertTrue(any(event["event"] == "action_proposed" for event in self.audit))

    def test_controlled_launch_requires_internal_finite_operation_deadline(self):
        for deadline in (None, True, float("nan"), float("inf"), self.now):
            with self.subTest(deadline=deadline), self.assertRaises(ValidationError):
                self.broker.submit(ActionProposal(
                    character="Testmage", command="lab-test-quick start",
                    expected_generation="session-1", controller_deadline=deadline,
                ))
        with self.assertRaises(ValidationError):
            ActionProposal.from_mapping({"character": "Testmage", "command": "lab-test-quick start",
                                         "controller_deadline": self.now + 30})

    def test_dispatched_launch_keeps_operation_deadline_and_exact_revocation(self):
        action = self.broker.submit(ActionProposal(
            character="Testmage", command="lab-test-quick start", ttl_seconds=2,
            expected_generation="session-1", expected_room_id="1000", controller_deadline=self.now + 30,
        ))
        self.broker.approve(ActionApproval(action_id=action["action_id"], character="Testmage",
                                          room_id="1000", generation="session-1"))
        self.broker.poll(self.context)
        self.now += 3
        observed = self.broker.get(action["action_id"])
        self.assertEqual(observed["status"], "dispatched")
        self.assertEqual(observed["controller_deadline"], 1030)
        self.assertFalse(observed["stop_requested"])
        with self.assertRaises(ValidationError):
            self.broker.revoke_controller_run(action["action_id"], character="Othermage", generation="session-1")
        self.broker.control(ActionControl(character="Testmage", generation="session-1", enabled=False))
        self.assertTrue(self.broker.get(action["action_id"])["stop_requested"])

    def test_expired_control_does_not_dispatch(self):
        action = self.submit()
        self.now += 3
        self.assertIsNone(self.broker.poll(self.context))
        self.assertEqual(self.broker.get(action["action_id"])["status"], "expired")

    def test_exact_cancel_prevents_dispatch_but_cannot_cancel_another_identity(self):
        action = self.submit()
        for character, generation in (("Othermage", "session-1"), ("Testmage", "session-2")):
            with self.assertRaises(ValidationError):
                self.broker.cancel(action["action_id"], character=character, generation=generation)
        self.broker.cancel(action["action_id"], character="Testmage", generation="session-1")
        self.assertIsNone(self.broker.poll(self.context))

    def test_generation_replacement_prevents_dispatch(self):
        action = self.submit()
        self.broker.admit_generation("Testmage", "session-2")
        self.assertIsNone(self.broker.poll(ActionContext("Testmage", "1000", "session-2")))
        self.assertEqual(self.broker.get(action["action_id"])["status"], "cancelled")

    def test_room_change_and_kill_switch_prevent_dispatch(self):
        action = self.submit()
        self.assertIsNone(self.broker.poll(ActionContext("Testmage", "1001", "session-1")))
        self.assertEqual(self.broker.get(action["action_id"])["status"], "denied_stale_room")
        action = self.submit()
        self.broker.control(ActionControl(character="Testmage", generation="session-1", enabled=False))
        self.assertIsNone(self.broker.poll(self.context))

    def test_even_status_control_cannot_hide_inside_a_command_sequence(self):
        with self.assertRaisesRegex(ValidationError, "cannot be batched"):
            self.broker.submit(ActionProposal(
                character="Testmage", commands=(f"lab-test-quick status {RUN_ID}", "look"),
                expected_generation="session-1", expected_room_id="1000",
            ))

    def test_dispatched_control_revocation_marks_cached_native_application_without_unsending(self):
        action = self.submit()
        self.broker.poll(self.context)
        self.broker.record_result(ActionResult(
            action_id=action["action_id"], character="Testmage", generation="session-1",
            outcome="sent_unverified", detail="queued locally; application unverified",
        ))
        for character, generation in (("Othermage", "session-1"), ("Testmage", "session-2")):
            with self.assertRaises(ValidationError):
                self.broker.revoke_controller_control(action["action_id"], character=character, generation=generation)
        revoked = self.broker.revoke_controller_control(
            action["action_id"], character="Testmage", generation="session-1",
        )
        self.assertEqual(revoked["status"], "completed")
        self.assertTrue(revoked["stop_requested"])
        self.assertEqual(revoked["completion"], "sent_unverified")
        unrelated = self.broker.submit(ActionProposal(character="Testmage", command="look"))
        with self.assertRaises(ValidationError):
            self.broker.revoke_controller_control(unrelated["action_id"], character="Testmage", generation="session-1")


class WaitingEvidence(FakeEvidence):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def verify_controller(self, registration, action_id, timeout_seconds):
        self.entered.set()
        if not self.release.wait(1):
            raise AssertionError("synthetic evidence wait was not released")
        return None


class ExactOperationControlTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.state, self.evidence = FakeClock(), FakeState(), WaitingEvidence()
        self.state.fresh, self.state.sequence = True, 1
        self.state.dead, self.state.stunned = False, False
        self.state.room_id, self.state.scripts = "1000", ()
        self.state.owners = {"movement": None, "combat": None}
        self.manifest = ControllerManifest.load(FIXTURE)
        self.broker = ActionBroker(policy=CommandPolicy(self.manifest), clock=self.clock)
        self.broker.admit_generation("Testmage", "generation-1")
        self.broker.control(ActionControl(character="Testmage", enabled=True))
        self.driver = BrokerDriver(self.broker, self.state)

        def launched(command):
            self.state.scripts = ("lab-test-quick", "bigshot")
            self.state.owners = {"movement": "lab-test-quick", "combat": "bigshot"}
            self.state.sequence += 1

        self.driver.after_command = launched
        self.runner = CapabilityRunner(
            actions=self.broker, state=self.state, evidence=self.evidence,
            controller_manifest=self.manifest, clock=self.clock, sleeper=self.clock.sleep,
            step_hook=self.driver,
        )
        self.operation = self.runner.start(
            "Testmage", "controller.quick", expected_generation="generation-1",
        )
        self.assertTrue(self.evidence.entered.wait(0.5), "launch did not reach terminal-evidence wait")

    def tearDown(self):
        self.evidence.release.set()
        self.runner.wait(self.operation.operation_id, timeout_seconds=0.5)

    def control(self, **overrides):
        arguments = {"character": "Testmage", "expected_generation": "generation-1", "control": "hold"}
        arguments.update(overrides)
        return self.runner.control_controller(self.operation.operation_id, **arguments)

    def test_attaches_control_to_existing_launch_without_creating_an_operation(self):
        result = self.control()
        self.assertEqual(result["operation_id"], self.operation.operation_id)
        self.assertEqual(result["action"]["command"], f"lab-test-quick hold {result['run_id']}")
        self.assertNotEqual(result["action"]["action_id"], result["run_id"])
        self.assertEqual(result["action"]["status"], "confirmation_required")
        self.assertIsNone(result["applied"])
        self.assertEqual(len(self.runner.history("Testmage")), 1)
        with self.assertRaisesRegex(ValidationError, "nonterminal operation"):
            self.runner.start("Testmage", "controller.quick")

    def test_exact_character_generation_and_registered_control_required(self):
        for override in ({"character": "Othermage"}, {"expected_generation": "old-session"},
                         {"control": "stop"}, {"control": "attack"}):
            with self.subTest(override=override), self.assertRaises(ValidationError):
                self.control(**override)

    def test_secondary_controller_action_does_not_replace_original_launch_binding(self):
        first = self.control(control="status")
        self.broker.cancel(first["action"]["action_id"], character="Testmage", generation="generation-1")
        self.runner._run_broker_step(
            self.operation, self.state.snapshot("Testmage"),
            f"lab-test-quick status {first['run_id']}", "synthetic secondary controller step", cleanup=True,
        )
        second = self.control()
        self.assertEqual(second["run_id"], first["run_id"])

    def test_missing_replaced_and_conflicting_owner_state_fails_closed(self):
        for owners, scripts in ((None, self.state.scripts),
                                ({"combat": "bigshot"}, self.state.scripts),
                                ({"movement": "go2", "combat": "bigshot"}, self.state.scripts),
                                (self.state.owners, ("bigshot",))):
            self.state.owners, self.state.scripts = owners, scripts
            with self.subTest(owners=owners, scripts=scripts), self.assertRaises(ValidationError):
                self.control()

    def test_stale_snapshot_and_session_change_reject_control(self):
        self.state.fresh = False
        with self.assertRaises(ValidationError):
            self.control()
        self.state.fresh, self.state.generation = True, "generation-2"
        with self.assertRaises(ValidationError):
            self.control()

    def test_interruption_cancels_only_owned_pending_controls_and_rejects_more(self):
        result = self.control()
        unrelated = self.broker.submit(ActionProposal(character="Testmage", command="look"))
        self.runner.interrupt(self.operation.operation_id)
        self.assertEqual(self.broker.get(result["action"]["action_id"])["status"], "cancelled")
        self.assertEqual(self.broker.get(unrelated["action_id"])["status"], "queued")
        self.assertTrue(self.broker.get(result["run_id"])["stop_requested"])
        with self.assertRaises(ValidationError):
            self.control(control="resume")

    def test_launch_deadline_is_exact_owning_operation_deadline(self):
        result = self.control(control="status")
        launch = self.broker.get(result["run_id"])
        self.assertEqual(launch["controller_deadline"], self.operation.deadline)

    def test_control_deadline_cannot_extend_owning_operation(self):
        self.clock.now = self.operation.deadline - 0.25
        result = self.control()
        self.assertEqual(result["action"]["expires_at"], self.operation.deadline)
        self.clock.now = self.operation.deadline
        self.assertEqual(self.broker.get(result["action"]["action_id"])["status"], "expired")
        with self.assertRaises(ValidationError):
            self.control()
