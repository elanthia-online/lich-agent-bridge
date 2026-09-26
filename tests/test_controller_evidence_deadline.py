"""Virtual-time replay of a controller returning after the first watch window."""
import unittest
from unittest.mock import patch

from lich_agent_bridge.actions import ActionBroker, ActionControl, CommandPolicy
from lich_agent_bridge.errors import ValidationError
from lich_agent_bridge.operations import CapabilityRunner, HandItem
from lich_agent_bridge.session_hub import WorldStateEvidenceAdapter
from lich_agent_bridge.world_state import WorldState
from .test_operations import BrokerDriver, FakeClock, FakeState
from .test_quick_area import load_manifest
from .test_refuge_outings import refuge_manifest_raw
from .test_session_hub import publish_controller_result, publish_snapshot


class ScheduledControllerWorld:
    """Emulate timed watch wakeups without sleeping or issuing commands."""
    def __init__(self, clock, state, result_after):
        self.clock, self.state = clock, state
        self.result_at = clock() + result_after
        self.action_id = None
        self.waits = []

    def snapshot(self, character):
        return {"snapshot": {"generation": self.state.generation}, "cursor": "0"}

    def watch(self, character, *, cursor, timeout):
        self.waits.append(timeout)
        if self.result_at > self.clock() + timeout:
            self.clock.sleep(timeout)
            return {"events": [], "cursor": str(cursor), "timed_out": True}
        self.clock.sleep(max(0, self.result_at - self.clock()))
        self.state.sequence += 1
        self.state.room_id = "1000"
        self.state.scripts = ()
        self.state.owners = {"movement": None, "combat": None}
        details = {"run_id": self.action_id, "cleanup_complete": True, "runtime": {
            "state": "completed", "work_result": {"state": "completed", "reason": "room_clear"},
            "refuge": {"room_id": 1000, "phase": "finished", "returned": True, "equipment_restored": True}}}
        return {"events": [{"kind": "controller_result", "generation": self.state.generation,
                            "data": {"controller": "quick", "action_id": self.action_id,
                                     "ok": True, "code": "quick_room_clear", "message": "Returned",
                                     "details": details}}], "cursor": "1", "timed_out": False}


class ControllerEvidenceDeadlineTests(unittest.TestCase):
    def run_outing(self, result_after):
        manifest = load_manifest(refuge_manifest_raw())
        clock, state = FakeClock(), FakeState()
        state.room_id, state.fresh, state.sequence = "1000", True, 2
        state.dead, state.stunned = False, False
        state.hands = {"left": None, "right": HandItem("777", "test weapon")}
        state.scripts, state.owners = (), {"movement": None, "combat": None}
        broker = ActionBroker(policy=CommandPolicy(manifest), clock=clock)
        broker.admit_generation("Testmage", "generation-1")
        broker.control(ActionControl(character="Testmage", enabled=True))
        world = ScheduledControllerWorld(clock, state, result_after=result_after)
        driver = BrokerDriver(broker, state)

        def launch(actions, proposal, snapshot):
            world.action_id = proposal["action_id"]
            driver(actions, proposal, snapshot)
            state.room_id = "1001"  # controller now owns an outing outside refuge

        runner = CapabilityRunner(actions=broker, state=state, evidence=WorldStateEvidenceAdapter(world),
                                  controller_manifest=manifest, clock=clock, sleeper=clock.sleep, step_hook=launch)
        with patch('lich_agent_bridge.session_hub.time') as timer:
            timer.monotonic.side_effect = clock
            result = runner.perform("Testmage", "controller.quick", expected_generation="generation-1", timeout_seconds=90)
        return result, clock, world, broker

    def test_return_after_first_watch_window_keeps_launch_authorized_until_result(self):
        result, clock, world, broker = self.run_outing(result_after=40)
        self.assertEqual(result.status, "succeeded", (result.explanation, world.waits,
                                                     broker.get(world.action_id).get("stop_requested")))
        self.assertEqual(result.end_state.room_id, "1000")
        self.assertEqual(result.alerts, [])
        self.assertEqual(clock(), 1040)
        self.assertTrue(all(wait <= 30 for wait in world.waits))

    def test_absent_return_evidence_still_revokes_at_operation_deadline(self):
        result, clock, world, broker = self.run_outing(result_after=120)
        self.assertEqual(result.status, "failed")
        self.assertIn("result evidence was not observed", result.explanation)
        self.assertTrue(result.alerts)
        self.assertTrue(broker.get(world.action_id)["stop_requested"])
        self.assertEqual(clock(), 1090)
        self.assertEqual(world.waits, [30, 30, 30])

    def test_real_watch_keeps_cursor_and_ignores_unrelated_results_between_windows(self):
        clock = FakeClock()
        world = WorldState(monotonic=clock)
        publish_snapshot(world)
        adapter = WorldStateEvidenceAdapter(world)
        registration = adapter.register_controller("operation-1", "hunt", "Testmage", "generation-1")
        scheduled = [(1010, "hunt", "other-action"), (1040, "other-controller", "action-1"),
                     (1065, "hunt", "action-1")]
        waits = []

        def wake(timeout):
            waits.append(timeout)
            if scheduled and scheduled[0][0] <= clock() + timeout:
                due, controller, action_id = scheduled.pop(0)
                clock.sleep(due - clock())
                publish_controller_result(world, controller=controller, action_id=action_id)
            else:
                clock.sleep(timeout)

        with patch.object(world._condition, "wait", side_effect=wake), \
                patch("lich_agent_bridge.session_hub.time") as timer:
            timer.monotonic.side_effect = clock
            evidence = adapter.verify_controller(registration, "action-1", 90)
        self.assertEqual(evidence.action_id, "action-1")
        self.assertEqual(evidence.object_id, "hunt")
        self.assertEqual(clock(), 1065)
        self.assertEqual(waits, [30, 30, 30])

    def test_real_watch_stops_at_total_deadline_with_bounded_individual_waits(self):
        for timeout, expected_waits in ((12, [12]), (65, [30, 30, 5]), (0, [])):
            with self.subTest(timeout=timeout):
                clock = FakeClock()
                world = WorldState(monotonic=clock)
                publish_snapshot(world)
                adapter = WorldStateEvidenceAdapter(world)
                registration = adapter.register_controller("operation-1", "hunt", "Testmage", "generation-1")
                with patch.object(world._condition, "wait", side_effect=clock.sleep) as wait, \
                        patch("lich_agent_bridge.session_hub.time") as timer:
                    timer.monotonic.side_effect = clock
                    self.assertIsNone(adapter.verify_controller(registration, "action-1", timeout))
                self.assertEqual([call.args[0] for call in wait.call_args_list], expected_waits)
                self.assertEqual(clock(), 1000 + timeout)

    def test_zero_wait_can_read_already_published_exact_result(self):
        world = WorldState()
        publish_snapshot(world)
        adapter = WorldStateEvidenceAdapter(world)
        registration = adapter.register_controller("operation-1", "hunt", "Testmage", "generation-1")
        publish_controller_result(world, action_id="action-1")
        self.assertEqual(adapter.verify_controller(registration, "action-1", 0).action_id, "action-1")

    def test_nonfinite_wait_is_rejected(self):
        world = WorldState()
        publish_snapshot(world)
        adapter = WorldStateEvidenceAdapter(world)
        registration = adapter.register_controller("operation-1", "hunt", "Testmage", "generation-1")
        for timeout in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValidationError, "finite"):
                adapter.verify_controller(registration, "action-1", timeout)
