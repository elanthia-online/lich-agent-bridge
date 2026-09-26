"""Synthetic direct-go2 contracts; never connects to a game."""
import unittest

from lich_agent_bridge.actions import ActionBroker, ActionControl
from lich_agent_bridge.operations import CapabilityRunner, HandItem
from .test_operations import BrokerDriver, FakeClock, FakeEvidence, FakeState
from .test_refuge_outings import refuge_manifest_raw
from .test_quick_area import load_manifest


class TravelTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.state = FakeClock(), FakeState()
        self.state.fresh, self.state.sequence = True, 2
        self.state.dead, self.state.stunned = False, False
        self.state.hands = {"left": None, "right": HandItem("777", "test weapon")}
        self.state.scripts = ()
        self.state.owners = dict.fromkeys(("movement", "combat", "inventory"))
        self.broker = ActionBroker(clock=self.clock)
        self.broker.admit_generation("Testmage", "generation-1")
        self.broker.control(ActionControl(character="Testmage", enabled=True))
        self.driver = BrokerDriver(self.broker, self.state)
        self.after_arrival = None
        self.arrive = True

        def step(broker, action, snapshot):
            self.driver(broker, action, snapshot)
            if self.arrive:
                self.state.room_id = "1000"
                self.state.sequence += 1
                self.state.script_status = {"go2": f"completed:{action['action_id']}"}
            if self.after_arrival:
                self.after_arrival(action)

        self.runner = CapabilityRunner(actions=self.broker, state=self.state, evidence=FakeEvidence(),
                                       clock=self.clock, sleeper=self.clock.sleep, step_hook=step)

    def travel(self, **overrides):
        args = {"expected_generation": "generation-1", "timeout_seconds": 60}
        args.update(overrides)
        return self.runner.perform("Testmage", "travel.go2", {"destination": "1000"}, **args)

    def test_arrival_requires_attributed_exit_and_original_hands(self):
        result = self.travel()
        self.assertEqual(result.status, "succeeded", result.explanation)
        self.assertEqual(self.driver.commands, ["go2 supervised 1000"])
        self.assertEqual(result.end_state.room_id, "1000")

    def test_noop_at_destination_uses_native_verification(self):
        self.state.room_id = "1000"
        self.assertEqual(self.travel().status, "succeeded")
        self.assertEqual(self.driver.commands, ["go2 supervised 1000"])

    def test_noop_at_destination_requires_attributed_native_completion(self):
        self.state.room_id = "1000"
        self.arrive = False
        self.assertEqual(self.travel().status, "failed")
        self.assertEqual(self.driver.commands, ["go2 supervised 1000"])

    def test_dispatch_is_not_arrival(self):
        self.arrive = False
        self.assertEqual(self.travel().status, "failed")

    def test_refuses_invalid_selectors_generation_budget_and_owners(self):
        for destination in ("4", "0", "-1", "town", "1000;look", True, 1000, "001000", "9" * 11):
            result = self.runner.perform("Testmage", "travel.go2", {"destination": destination}, expected_generation="generation-1")
            self.assertEqual(result.status, "failed", destination)
        for options in ({"expected_generation": None}, {"expected_generation": "old"}, {"timeout_seconds": 121}):
            self.assertEqual(self.travel(**options).status, "failed")
        for lane in self.state.owners:
            self.state.owners[lane] = "foreign"
            self.assertEqual(self.travel().status, "failed", lane)
            self.state.owners[lane] = None
        self.state.scripts = ("go2",)
        self.assertEqual(self.travel().status, "failed")
        self.assertEqual(self.driver.commands, [])

    def test_wrong_equipment_or_unattributed_completion_is_not_success(self):
        for field, value in (("hands", {"left": None, "right": None}), ("script_status", {"go2": "completed:other"}),
                             ("scripts", ("go2",)), ("dead", True), ("fresh", False)):
            self.setUp()
            self.after_arrival = lambda _action: setattr(self.state, field, value)
            self.assertEqual(self.travel().status, "failed", field)

    def test_unresolved_outing_only_permits_its_refuge_and_does_not_forget_wrong_hands(self):
        controller = load_manifest(refuge_manifest_raw()).controllers[0]
        before = self.state.snapshot("Testmage")
        self.runner._refuge_pending["testmage"] = ("old-run", controller, before, "old-action")
        rejected = self.runner.perform("Testmage", "travel.go2", {"destination": "1001"}, expected_generation="generation-1")
        self.assertEqual(rejected.status, "failed")
        self.assertEqual(self.driver.commands, [])
        self.assertEqual(self.travel().status, "succeeded")
        self.assertFalse(self.runner._refuge_pending)

    def test_stop_during_dispatched_travel_sets_exact_revocation_marker(self):
        from lich_agent_bridge.actions import ActionApproval, ActionContext
        def step(broker, action, snapshot):
            request = broker.poll(ActionContext(character="Testmage", room_id=snapshot.room_id))
            broker.approve(ActionApproval(action_id=request["action_id"], character="Testmage", room_id=snapshot.room_id, approval_mode="auto"))
            broker.poll(ActionContext(character="Testmage", room_id=snapshot.room_id))
            self.runner.interrupt(self.runner.history()[-1].operation_id)
            self.assertTrue(broker.get(action["action_id"])["stop_requested"])
        self.runner._step_hook = step
        self.assertEqual(self.travel().status, "interrupted")

    def test_final_stop_or_deadline_cannot_be_reported_as_success(self):
        self.after_arrival = lambda _action: self.runner.interrupt(self.runner.history()[-1].operation_id)
        self.assertEqual(self.travel().status, "interrupted")
        self.setUp()
        self.after_arrival = lambda _action: self.clock.sleep(61)
        self.assertEqual(self.travel().status, "timed_out")

    def test_actions_off_and_generation_replacement_revoke_dispatched_go2(self):
        from lich_agent_bridge.actions import ActionApproval, ActionContext, ActionProposal
        for change in (lambda: self.broker.control(ActionControl(character="Testmage", enabled=False)),
                       lambda: self.broker.admit_generation("Testmage", "new")):
            self.setUp()
            action = self.broker.submit(ActionProposal(character="Testmage", command="go2 supervised 1000", expected_room_id="1234", expected_generation="generation-1"))
            self.broker.poll(ActionContext(character="Testmage", room_id="1234"))
            self.broker.approve(ActionApproval(action_id=action["action_id"], character="Testmage", room_id="1234", approval_mode="auto"))
            self.broker.poll(ActionContext(character="Testmage", room_id="1234"))
            change()
            self.assertTrue(self.broker.get(action["action_id"])["stop_requested"])
