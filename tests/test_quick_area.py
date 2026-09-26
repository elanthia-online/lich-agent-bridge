"""Synthetic profile-area contracts; Bigshot alone resolves room membership."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from lich_agent_bridge.actions import ActionBroker, ActionControl, CommandPolicy
from lich_agent_bridge.controller_manifest import ControllerManifest
from lich_agent_bridge.errors import ConfigurationError
from lich_agent_bridge.operations import CapabilityRunner, EvidenceRecord, _OperationAbort
from .test_operations import BrokerDriver, FakeClock, FakeEvidence, FakeState


FIXTURE = Path(__file__).parent / "fixtures/controller-controls.json"
RUN_ID = "0123456789abcdef"


def area_manifest_raw():
    raw = json.loads(FIXTURE.read_text())
    controller = raw["controllers"][0]
    controller.update(script="bigshot", safe_handoff={"kind": "quick_area"},
                      control_owner_scripts=["bigshot"])
    controller["actions"][0]["script_args_template"] = "quick watch --area profile"
    return raw


def load_manifest(raw):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "controllers.json"
        path.write_text(json.dumps(raw))
        return ControllerManifest.load(path)


class QuickAreaTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest(area_manifest_raw())
        self.controller = self.manifest.controller("quick")
        self.state = FakeState()
        self.state.fresh, self.state.sequence = True, 2
        self.state.dead, self.state.stunned = False, False
        self.state.room_id, self.state.scripts = "1001", ()
        self.state.owners = {"movement": None, "combat": None}
        self.facts = {"ok": True, "code": "quick_completed", "message": "Synthetic field result",
                      "details": {"run_id": RUN_ID, "cleanup_complete": True,
                                  "runtime": {"mode": "watch", "state": "completed",
                                              "area": {"kind": "profile", "start_room_id": 1000,
                                                       "boundary_room_ids": [1009], "room_count": 3,
                                                       "room_id": 1001, "in_bounds": True}}}}

    def evidence(self):
        return EvidenceRecord(method="controller.quick", generation="generation-1", object_id="quick",
                              action_id=RUN_ID, detail="Synthetic area proof", facts=self.facts)

    def handoff(self):
        evidence = self.evidence()
        CapabilityRunner._verify_controller_evidence(evidence, controller=self.controller,
                                                     generation="generation-1", action_id=RUN_ID)
        CapabilityRunner._verify_controller_handoff(self.state.snapshot("Testmage"), self.controller, {},
                                                    evidence=evidence, launch_room_id="1000")

    def test_registration_requires_native_controls_and_explicit_fixed_area(self):
        self.assertEqual(self.controller.safe_handoff, {"kind": "quick_area"})
        mutations = [
            lambda c: c.update(script="lab-test-quick"),
            lambda c: c.update(control_owner_scripts=[]),
            lambda c: c["safe_handoff"].update(rooms={"test": "1000"}),
            lambda c: c["actions"][0].update(script_args_template="quick use reviewed"),
            lambda c: c["actions"][0].update(script_args_template="quick watch --area off"),
            lambda c: c["actions"][0].update(script_args_template="quick watch --area profile --area off"),
            lambda c: c["actions"][0].update(script_args_template="quick watch --area=profile"),
            lambda c: c["actions"][0].update(script_args_template="quick watch -- --area profile"),
            lambda c: c["actions"][0].update(command_template="lab-test-quick start{extra}",
                script_args_template="quick watch --area profile {extra}",
                parameters=[{"name": "extra", "type": "flag_suffix", "true_value": " --area off"}]),
        ]
        for mutation in mutations:
            raw = area_manifest_raw()
            mutation(raw["controllers"][0])
            with self.subTest(raw=raw), self.assertRaises(ConfigurationError):
                load_manifest(raw)

    def test_watch_assist_handoff_uses_terminal_membership_without_room_traversal(self):
        for mode in ("watch", "assist", "seek"):
            self.facts["details"]["runtime"]["mode"] = mode
            self.handoff()

    def test_seek_registration_requires_explicit_area_and_movement_ownership(self):
        for mode in ("seek", "SEEK", "{mode}"):
            raw = area_manifest_raw()
            controller = raw["controllers"][0]
            launch = controller["actions"][0]
            launch.update(script_args_template=f"quick {mode} --area profile",
                          command_template=f"bigshot quick {mode} --area profile")
            if mode == "{mode}":
                launch["parameters"] = [{"name": "mode", "type": "enum", "values": ["clear", "seek"]}]
            self.assertIsNotNone(load_manifest(raw))
            for change in (lambda c: c.update(lanes=["combat"]),
                           lambda c: c.update(lanes=["movement"]),
                           lambda c: c.update(safe_handoff={"kind": "room", "room_id": "1000"})):
                changed = deepcopy(raw)
                change(changed["controllers"][0])
                with self.subTest(mode=mode), self.assertRaisesRegex(ConfigurationError, "seek requires"):
                    load_manifest(changed)

    def test_clear_trial_cannot_handoff_after_movement(self):
        for mode in ("clear", "trial"):
            self.facts["details"]["runtime"]["mode"] = mode
            with self.assertRaises(_OperationAbort):
                self.handoff()
            self.state.room_id = "1000"
            self.facts["details"]["runtime"]["area"]["room_id"] = 1000
            self.handoff()
            self.state.room_id = "1001"
            self.facts["details"]["runtime"]["area"]["room_id"] = 1001

    def test_handoff_rejects_missing_stale_malformed_and_uncorrelated_area_proof(self):
        original = deepcopy(self.facts)
        mutations = [
            lambda d: d.update(run_id="ffffffffffffffff"),
            lambda d: d.update(cleanup_complete=False),
            lambda d: d.pop("runtime"),
            lambda d: d["runtime"].update(state="running"),
            lambda d: d["runtime"].update(mode="unknown"),
            lambda d: d["runtime"].pop("area"),
            lambda d: d["runtime"]["area"].update(in_bounds=False),
            lambda d: d["runtime"]["area"].update(in_bounds="true"),
            lambda d: d["runtime"]["area"].update(room_id=1000),
            lambda d: d["runtime"]["area"].update(room_id=True),
            lambda d: d["runtime"]["area"].update(room_count=0),
            lambda d: d["runtime"]["area"].update(boundary_room_ids=["1009"]),
        ]
        for mutation in mutations:
            self.facts = deepcopy(original)
            mutation(self.facts["details"])
            with self.subTest(facts=self.facts), self.assertRaises(_OperationAbort):
                self.handoff()

    def test_handoff_requires_survival_and_all_owners_exited(self):
        for field, value in (("dead", True), ("dead", None), ("stunned", True),
                             ("stunned", None), ("scripts", None), ("scripts", ("bigshot",)),
                             ("owners", {"movement": "bigshot", "combat": None})):
            old = getattr(self.state, field)
            setattr(self.state, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(_OperationAbort):
                self.handoff()
            setattr(self.state, field, old)

    def test_actual_runner_refuses_legacy_field_only_handoff(self):
        self.assert_runner_field_handoff()

    def test_actual_seek_runner_refuses_legacy_field_only_handoff(self):
        raw = area_manifest_raw()
        raw["controllers"][0]["actions"][0].update(
            command_template="bigshot quick seek --area profile",
            script_args_template="quick seek --area profile")
        self.manifest = load_manifest(raw)
        self.controller = self.manifest.controller("quick")
        self.facts["details"]["runtime"]["mode"] = "seek"
        self.assert_runner_field_handoff()

    def test_shipped_seek_example_is_opt_in_and_uses_a_finite_preset_enum(self):
        manifest = ControllerManifest.load(Path(__file__).parents[1] / "examples/controllers/bigshot-quick-seek.json")
        controller = manifest.controller("quick-seek")
        self.assertEqual(controller.safe_handoff["kind"], "quick_refuge")
        self.assertEqual(set(controller.lanes), {"movement", "combat", "inventory"})

    def assert_runner_field_handoff(self):
        clock, evidence = FakeClock(), FakeEvidence()
        self.state.room_id = "1000"
        broker = ActionBroker(policy=CommandPolicy(self.manifest), clock=clock)
        broker.admit_generation("Testmage", "generation-1")
        broker.control(ActionControl(character="Testmage", enabled=True))
        driver = BrokerDriver(broker, self.state)
        def completed(command):
            self.state.room_id = "1001"
            self.state.sequence += 1
        driver.after_command = completed
        evidence.controller_result = self.facts
        original_verify = evidence.verify_controller
        def verify(registration, action_id, timeout_seconds):
            self.facts["details"]["run_id"] = action_id
            return original_verify(registration, action_id, timeout_seconds)
        evidence.verify_controller = verify
        runner = CapabilityRunner(actions=broker, state=self.state, evidence=evidence,
                                  controller_manifest=self.manifest, clock=clock,
                                  sleeper=clock.sleep, step_hook=driver)
        operation = runner.perform("Testmage", "controller.quick", expected_generation="generation-1")
        self.assertEqual(operation.status, "failed", operation.explanation)
        self.assertIn("quick_refuge", operation.explanation)
        self.assertEqual(driver.commands, [])
