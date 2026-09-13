import re
import threading
import time
import unittest
from pathlib import Path

from lich_agent_bridge.actions import (
    ActionApproval,
    ActionBroker,
    ActionContext,
    ActionControl,
    ActionResult,
    CommandPolicy,
)
from lich_agent_bridge.controller_manifest import ControllerManifest
from lich_agent_bridge.operations import (
    CapabilityRunner,
    EvidenceRecord,
    HandItem,
    ItemState,
    NearbyObject,
    OwnedLocation,
    SessionState,
    SUPPORTED_ITEM_AUDIT_METHODS,
)
from lich_agent_bridge.errors import ValidationError
from .synthetic_profiles import TEST_PROFILES


def fixture_manifest():
    return ControllerManifest.load(Path(__file__).parent / "fixtures" / "controllers.json")


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeState:
    def __init__(self):
        self.original = OwnedLocation(
            location_id="container:#200",
            kind="container",
            owner="Testmage",
            verified_owned=True,
            safety_rank=10,
            restore_command="put #100 in #200",
        )
        self.hand = OwnedLocation(
            location_id="right_hand",
            kind="right_hand",
            owner="Testmage",
            verified_owned=True,
            safety_rank=5,
        )
        self.vault = OwnedLocation(
            location_id="container:#300",
            kind="container",
            owner="Testmage",
            verified_owned=True,
            safety_rank=20,
            restore_command="put #100 in #300",
        )
        self.generation = "generation-1"
        self.room_id = "1234"
        self.fresh = None
        self.sequence = None
        self.dead = None
        self.stunned = None
        self.wounds = None
        self.encumbrance = None
        self.hands = None
        self.scripts = None
        self.owners = None
        self.nearby_creatures = None
        self.nearby_corpses = None
        self.script_status = None
        self.items = [
            ItemState(
                object_id="100",
                dossier_id="dossier-7",
                fingerprint="fingerprint-9",
                location=self.original,
            )
        ]

    def snapshot(self, character):
        return SessionState(
            character=character,
            generation=self.generation,
            room_id=self.room_id,
            items=tuple(self.items),
            fresh=self.fresh,
            sequence=self.sequence,
            dead=self.dead,
            stunned=self.stunned,
            wounds=self.wounds,
            encumbrance=self.encumbrance,
            hands=self.hands,
            scripts=self.scripts,
            owners=self.owners,
            nearby_creatures=self.nearby_creatures,
            nearby_corpses=self.nearby_corpses,
            script_status=self.script_status,
        )

    def safest_owned_locations(self, character, object_id):
        return (self.hand, self.original, self.vault)

    def move(self, location):
        current = self.items[0]
        self.items[0] = ItemState(
            object_id=current.object_id,
            dossier_id=current.dossier_id,
            fingerprint=current.fingerprint,
            location=location,
        )


class FakeEvidence:
    def __init__(self):
        self.registrations = []
        self.fail_methods = set()
        self.controller_result = None
        self.controller_recovery_result = None
        self.controller_recovery_receipt_result = None

    def register(self, operation_id, method, binding):
        token = (operation_id, method, binding.generation, binding.object_id)
        self.registrations.append(token)
        return token

    def verify(self, registration, action_id=None):
        _, method, generation, object_id = registration
        if method in self.fail_methods:
            return None
        return EvidenceRecord(
            method=method,
            generation=generation,
            object_id=object_id,
            detail=f"verified {method}",
            facts={"method": method},
            action_id=action_id,
        )

    def register_controller(self, operation_id, controller, character, generation):
        token = (operation_id, controller, character, generation)
        self.registrations.append(token)
        return token

    def verify_controller(self, registration, action_id, timeout_seconds):
        if self.controller_result is None:
            return None
        _, controller, _, generation = registration
        result = self.controller_result
        return EvidenceRecord(
            method=f"controller.{controller}",
            generation=generation,
            object_id=controller,
            detail=result["message"],
            facts=dict(result),
            action_id=action_id,
        )

    def verify_controller_recovery(self, **_details):
        return self.controller_recovery_result

    def controller_recovery_receipt(self, **_details):
        return self.controller_recovery_receipt_result


class BrokerDriver:
    def __init__(self, broker, state):
        self.broker = broker
        self.state = state
        self.commands = []
        self.no_result_for = set()
        self.fail_for = set()
        self.skip_state_update_for = set()
        self.retain_corpse_ids = set()
        self.after_command = None

    def __call__(self, broker, proposal, snapshot):
        command = proposal["command"]
        self.commands.append(command)
        if command in self.no_result_for:
            return
        action = broker.poll(
            ActionContext(character=snapshot.character, room_id=snapshot.room_id, generation=snapshot.generation)
        )
        if action["status"] == "confirmation_required":
            broker.approve(
                ActionApproval(
                    action_id=action["action_id"],
                    character=snapshot.character,
                    room_id=snapshot.room_id,
                    approval_mode="auto",
                    generation=snapshot.generation,
                )
            )
            action = broker.poll(
                ActionContext(character=snapshot.character, room_id=snapshot.room_id, generation=snapshot.generation)
            )
        if command in self.fail_for:
            outcome = "failed"
        else:
            outcome = "completed"
            if command in self.skip_state_update_for:
                pass
            elif command == "get #100":
                self.state.move(self.state.hand)
            elif command == "put #100 in #200":
                self.state.move(self.state.original)
            elif command == "put #100 in #300":
                self.state.move(self.state.vault)
            elif command == "bigshot start":
                self.state.sequence += 1
                self.state.scripts = tuple(
                    sorted(set(self.state.scripts or ()).union({"bigshot"}))
                )
                self.state.owners = {
                    **dict(self.state.owners or {}),
                    "movement": "bigshot",
                    "combat": "bigshot",
                }
                self.state.script_status = {
                    **dict(self.state.script_status or {}),
                    "bigshot": "running",
                }
            elif command == "eloot loot":
                self.state.sequence += 1
                self.state.nearby_corpses = tuple(
                    corpse
                    for corpse in self.state.nearby_corpses or ()
                    if corpse.object_id in self.retain_corpse_ids
                )
                self.state.scripts = tuple(
                    name
                    for name in self.state.scripts or ()
                    if name.casefold() != "eloot"
                )
                self.state.owners = {
                    **dict(self.state.owners or {}),
                    "inventory": None,
                }
                self.state.script_status = {
                    **dict(self.state.script_status or {}),
                    "eloot": f"completed:{action['action_id']}",
                }
            elif re.fullmatch(r"lab recover [0-9a-f]{16} confirm", command):
                self.state.sequence += 1
        broker.record_result(
            ActionResult(
                action_id=action["action_id"],
                character=snapshot.character,
                outcome=outcome,
                detail="command sent" if outcome == "completed" else "rejected",
                generation=snapshot.generation,
            )
        )
        if self.after_command is not None:
            self.after_command(command)


class BlockingBrokerDriver(BrokerDriver):
    def __init__(self, broker, state):
        super().__init__(broker, state)
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, broker, proposal, snapshot):
        self.entered.set()
        if not self.release.wait(2):
            raise TimeoutError("test driver was not released")
        super().__call__(broker, proposal, snapshot)


class CapabilityRunnerTests(unittest.TestCase):
    def test_default_hunting_has_no_personal_profile_and_fails_before_commands(self):
        runner = CapabilityRunner(actions=self.broker, state=self.state,
                                  evidence=self.evidence, step_hook=self.driver)
        catalog = {item["name"]: item for item in runner.describe_capabilities("Testmage")}
        self.assertFalse(catalog["hunt.prepare"]["available"])
        self.assertEqual(catalog["hunt.prepare"]["supported_characters"], [])
        result = runner.perform("Testmage", "hunt.prepare")
        self.assertEqual(result.status, "failed")
        self.assertIn("no profile", result.explanation)
        self.assertEqual(self.driver.commands, [])

    def test_capability_catalog_describes_dispatch_and_character_availability(self):
        runner = CapabilityRunner(
            actions=ActionBroker(),
            state=FakeState(),
            evidence=FakeEvidence(),
            controller_manifest=fixture_manifest(),
            profiles=TEST_PROFILES,
        )

        catalog = {item["name"]: item for item in runner.describe_capabilities("Testknight")}

        self.assertEqual(
            set(catalog),
            {
                "travel.go2",
                "character.recon",
                "item.audit",
                "hunt.prepare",
                "room.loot",
                "session.recover_controller",
                "controller.engage",
                "controller.room",
                "controller.hunt",
                "controller.rift",
            },
        )
        self.assertTrue(catalog["item.audit"]["available"])
        self.assertFalse(catalog["hunt.prepare"]["available"])
        self.assertTrue(catalog["controller.hunt"]["available"])
        self.assertEqual(
            catalog["controller.hunt"]["arguments"]["properties"]["profile"]["enum"],
            ["test-hunt", "test-probe"],
        )
        self.assertFalse(catalog["item.audit"]["arguments"]["additionalProperties"])
        self.assertEqual(
            catalog["item.audit"]["arguments"]["properties"]["methods"]["items"]["enum"],
            list(SUPPORTED_ITEM_AUDIT_METHODS),
        )

    def test_terminal_operation_records_end_to_end_timing(self):
        recorded = []
        clock = FakeClock()
        actions = ActionBroker(clock=clock)
        state = FakeState()
        runner = CapabilityRunner(
            actions=actions,
            state=state,
            evidence=FakeEvidence(),
            clock=clock,
            sleeper=clock.sleep,
            timing=recorded.append,
        )

        operation = runner.perform("Testmage", "unsupported")

        self.assertEqual(operation.status, "failed")
        self.assertEqual(recorded[0]["metric"], "operation.end_to_end_ms")
        self.assertEqual(recorded[0]["capability"], "unsupported")
        self.assertEqual(recorded[0]["status"], "failed")

    def setUp(self):
        self.clock = FakeClock()
        self.state = FakeState()
        self.evidence = FakeEvidence()
        self.audit = []
        self.alerts = []
        self.broker = ActionBroker(audit=self.audit.append, clock=self.clock,
                                   policy=CommandPolicy(fixture_manifest()))
        self.broker.admit_generation("Testmage", "generation-1")
        self.broker.control(ActionControl(character="Testmage", enabled=True))
        self.driver = BrokerDriver(self.broker, self.state)
        self.runner = CapabilityRunner(
            actions=self.broker,
            state=self.state,
            evidence=self.evidence,
            clock=self.clock,
            sleeper=self.clock.sleep,
            step_hook=self.driver,
            alert=self.alerts.append,
            history_limit=3,
            controller_manifest=fixture_manifest(),
            profiles=TEST_PROFILES,
        )

    def perform(self, methods=("look", "inspect", "405", "735"), timeout=10):
        return self.runner.perform(
            "Testmage",
            "item.audit",
            {"item_id": "100", "methods": list(methods)},
            timeout_seconds=timeout,
        )

    def blocking_runner(self):
        driver = BlockingBrokerDriver(self.broker, self.state)
        runner = CapabilityRunner(
            actions=self.broker,
            state=self.state,
            evidence=self.evidence,
            clock=self.clock,
            sleeper=self.clock.sleep,
            step_hook=driver,
            alert=self.alerts.append,
            history_limit=3,
            controller_manifest=fixture_manifest(),
            profiles=TEST_PROFILES,
        )
        return runner, driver

    def configure_hunt(self, character="Testmage"):
        self.state.fresh = True
        self.state.sequence = 1
        self.state.dead = False
        self.state.stunned = False
        self.state.wounds = {}
        self.state.encumbrance = 0
        name = (
            "a plain ash staff"
            if character.casefold() == "testmage"
            else "a iron hammer"
        )
        self.state.hands = {
            "right": HandItem(object_id="weapon-1", name=name),
            "left": None,
        }
        self.state.scripts = ()
        self.state.owners = {
            "movement": None,
            "combat": None,
            "inventory": None,
        }
        self.state.script_status = {}

    def configure_room_loot(self):
        self.state.fresh = True
        self.state.sequence = 1
        self.state.scripts = ()
        self.state.owners = {
            "movement": None,
            "combat": None,
            "inventory": None,
        }
        self.state.nearby_creatures = ()
        self.state.nearby_corpses = (
            NearbyObject(object_id="corpse-1", noun="mastiff"),
            NearbyObject(object_id="corpse-2", noun="giant"),
        )
        self.state.script_status = {}

    def configure_controller_hunt(self):
        self.broker.admit_generation("Testknight", "generation-1")
        self.broker.control(ActionControl(character="Testknight", enabled=True))
        self.state.fresh = True
        self.state.sequence = 1
        self.state.room_id = "1000"
        self.state.dead = False
        self.state.stunned = False
        self.state.wounds = {}
        self.state.scripts = ()
        self.state.owners = {
            "movement": None,
            "combat": None,
            "inventory": None,
        }
        self.state.script_status = {}
        self.evidence.controller_result = {
            "ok": True,
            "code": "returned",
            "message": "hunt completed in safe room",
            "details": {"room_id": 1000},
        }

        def complete(command):
            if command == "lab-test-hunt start test-hunt":
                self.state.sequence += 1
                self.state.room_id = "1000"
                self.state.scripts = ()
                self.state.owners = {
                    **dict(self.state.owners),
                    "movement": None,
                    "combat": None,
                }

        self.driver.after_command = complete

    def wait_terminal(self, runner, operation_id, *, timeout=1):
        deadline = time.monotonic() + timeout
        cursor = 0
        while time.monotonic() < deadline:
            events, operation = runner.wait(
                operation_id,
                after_cursor=cursor,
                timeout_seconds=min(0.2, deadline - time.monotonic()),
            )
            if events:
                cursor = events[-1].cursor
            if operation.status in {
                "succeeded",
                "failed",
                "timed_out",
                "interrupted",
            }:
                return operation
        self.fail("operation did not reach a terminal state")

    def test_happy_path_uses_exact_id_evidence_and_same_id_restore(self):
        operation = self.perform()

        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(
            self.driver.commands,
            [
                "look #100",
                "inspect #100",
                "get #100",
                "diagnose item 405 exact #100",
                "diagnose item 735 exact #100",
                "put #100 in #200",
            ],
        )
        self.assertEqual(
            [item.method for item in operation.evidence],
            ["look", "inspect", "405", "735"],
        )
        self.assertEqual(self.state.items[0].location, self.state.original)
        self.assertEqual(
            [event.status for event in operation.events if event.detail in {
                "operation requested",
                "item identity and session admitted",
                "item audit running",
                operation.explanation,
            }],
            ["requested", "admitted", "running", "succeeded"],
        )
        self.assertTrue(
            all(
                event["completion"] == "sent_unverified"
                for event in self.audit
                if event["event"] == "action_result"
            )
        )

    def test_unsupported_method_fails_explicitly_without_action(self):
        for method in ("loresong", "info_delta"):
            with self.subTest(method=method):
                operation = self.perform((method,))
                self.assertEqual(operation.status, "failed")
                self.assertIn("unsupported item.audit method", operation.explanation)
        self.assertEqual(self.driver.commands, [])

    def test_stale_generation_fails_before_next_step(self):
        self.driver.after_command = lambda command: setattr(
            self.state, "generation", "generation-2"
        ) if command == "look #100" else None
        operation = self.perform(("look", "inspect"))
        self.assertEqual(operation.status, "failed")
        self.assertIn("stale session generation", operation.explanation)
        self.assertEqual(self.driver.commands, ["look #100"])

    def test_ambiguous_identity_fails_closed(self):
        self.state.items.append(self.state.items[0])
        operation = self.perform(("look",))
        self.assertEqual(operation.status, "failed")
        self.assertIn("ambiguous item identity", operation.explanation)
        self.assertEqual(self.driver.commands, [])

    def test_broker_denial_fails_without_dispatch(self):
        self.broker.control(ActionControl(character="Testmage", enabled=False))
        operation = self.perform(("look",))
        self.assertEqual(operation.status, "failed")
        self.assertIn("broker denied", operation.explanation)
        self.assertEqual(self.driver.commands, [])

    def test_registered_evidence_failure_is_not_success(self):
        self.evidence.fail_methods.add("inspect")
        operation = self.perform(("look", "inspect"))
        self.assertEqual(operation.status, "failed")
        self.assertIn("registered inspect evidence was not observed", operation.explanation)
        self.assertEqual(self.state.items[0].location, self.state.original)
        self.assertNotIn("put #100 in #300", self.driver.commands)

    def test_restore_failure_secures_safest_supplied_owned_location_and_alerts(self):
        self.driver.fail_for.add("put #100 in #200")
        operation = self.perform(("405",))
        self.assertEqual(operation.status, "failed")
        self.assertIn("restore failed", operation.explanation)
        self.assertEqual(self.state.items[0].location, self.state.vault)
        self.assertEqual(self.driver.commands[-1], "put #100 in #300")
        self.assertTrue(operation.alerts)
        self.assertTrue(self.alerts)

    def test_restore_never_falls_back_to_a_noun_command(self):
        self.state.original = OwnedLocation(
            location_id="container:#200",
            kind="container",
            owner="Testmage",
            verified_owned=True,
            safety_rank=10,
            restore_command="put my crown in #200",
        )
        self.state.move(self.state.original)
        operation = self.perform(("405",))
        self.assertEqual(operation.status, "failed")
        self.assertIn("does not reference exact object #100", operation.explanation)
        self.assertNotIn("put my crown in #200", self.driver.commands)
        self.assertEqual(self.state.items[0].location, self.state.vault)

    def test_interruption_prevents_next_diagnostic_and_restores(self):
        def interrupt_after_take(command):
            if command == "get #100":
                self.runner.interrupt(self.runner.history()[-1].operation_id)

        self.driver.after_command = interrupt_after_take
        operation = self.perform(("405",))
        self.assertEqual(operation.status, "interrupted")
        self.assertEqual(self.driver.commands, ["get #100", "put #100 in #300"])
        self.assertNotIn("diagnose item 405 exact #100", self.driver.commands)
        self.assertEqual(self.state.items[0].location, self.state.vault)

    def test_timeout_is_terminal_and_issues_no_cleanup_command_after_deadline(self):
        self.driver.no_result_for.add("look #100")
        operation = self.perform(("look",), timeout=0.03)
        self.assertEqual(operation.status, "timed_out")
        self.assertEqual(self.driver.commands, ["look #100"])

    def test_history_is_bounded_and_event_cursors_resume(self):
        operations = [self.perform(("look",)) for _ in range(4)]
        self.assertEqual(len(self.runner.history()), 3)
        with self.assertRaisesRegex(Exception, "operation was not found"):
            self.runner.get(operations[0].operation_id)
        latest = operations[-1]
        cursor = latest.events[1].cursor
        resumed = self.runner.events(latest.operation_id, after_cursor=cursor)
        self.assertTrue(resumed)
        self.assertTrue(all(event.cursor > cursor for event in resumed))

    def test_start_returns_immediately_with_known_operation_id(self):
        runner, driver = self.blocking_runner()
        started_at = time.monotonic()
        operation = runner.start(
            "Testmage",
            "item.audit",
            {"item_id": "100", "methods": ["look"]},
            timeout_seconds=10,
        )
        elapsed = time.monotonic() - started_at
        try:
            self.assertLess(elapsed, 0.25)
            self.assertRegex(operation.operation_id, r"\A[0-9a-f]{16}\Z")
            self.assertIs(runner.get(operation.operation_id), operation)
            self.assertTrue(driver.entered.wait(1))
            self.assertNotIn(operation.status, {"succeeded", "failed"})
        finally:
            driver.release.set()
        self.assertEqual(
            self.wait_terminal(runner, operation.operation_id).status, "succeeded"
        )

    def test_wait_wakes_when_new_progress_is_emitted(self):
        runner, driver = self.blocking_runner()
        operation = runner.start(
            "Testmage",
            "item.audit",
            {"item_id": "100", "methods": ["look"]},
            timeout_seconds=10,
        )
        self.assertTrue(driver.entered.wait(1))
        cursor = runner.events(operation.operation_id)[-1].cursor
        result = []

        waiter = threading.Thread(
            target=lambda: result.append(
                runner.wait(
                    operation.operation_id,
                    after_cursor=cursor,
                    timeout_seconds=1,
                )
            )
        )
        waiter.start()
        time.sleep(0.02)
        driver.release.set()
        waiter.join(timeout=1)

        self.assertFalse(waiter.is_alive())
        self.assertTrue(result[0][0])
        self.assertTrue(all(event.cursor > cursor for event in result[0][0]))
        self.wait_terminal(runner, operation.operation_id)

    def test_wait_returns_terminal_record(self):
        runner, driver = self.blocking_runner()
        operation = runner.start(
            "Testmage",
            "item.audit",
            {"item_id": "100", "methods": ["look"]},
            timeout_seconds=10,
        )
        self.assertTrue(driver.entered.wait(1))
        driver.release.set()
        terminal = self.wait_terminal(runner, operation.operation_id)
        events, observed = runner.wait(
            operation.operation_id,
            after_cursor=terminal.events[-1].cursor,
            timeout_seconds=1,
        )
        self.assertEqual(events, ())
        self.assertIs(observed, terminal)
        self.assertEqual(observed.status, "succeeded")

    def test_wait_timeout_does_not_time_out_operation(self):
        runner, driver = self.blocking_runner()
        operation = runner.start(
            "Testmage",
            "item.audit",
            {"item_id": "100", "methods": ["look"]},
            timeout_seconds=10,
        )
        self.assertTrue(driver.entered.wait(1))
        cursor = runner.events(operation.operation_id)[-1].cursor
        events, observed = runner.wait(
            operation.operation_id,
            after_cursor=cursor,
            timeout_seconds=0.01,
        )
        try:
            self.assertEqual(events, ())
            self.assertEqual(observed.status, "running")
            self.assertNotEqual(observed.status, "timed_out")
        finally:
            driver.release.set()
        self.wait_terminal(runner, operation.operation_id)

    def test_started_operation_can_be_interrupted(self):
        runner, driver = self.blocking_runner()
        operation = runner.start(
            "Testmage",
            "item.audit",
            {"item_id": "100", "methods": ["look", "inspect"]},
            timeout_seconds=10,
        )
        self.assertTrue(driver.entered.wait(1))
        runner.interrupt(operation.operation_id)
        driver.release.set()
        terminal = self.wait_terminal(runner, operation.operation_id)

        self.assertEqual(terminal.status, "interrupted")
        self.assertEqual(driver.commands, ["look #100"])
        self.assertIn("interrupted before next step", terminal.explanation)

    def test_background_adapter_exception_becomes_terminal_failure(self):
        def explode(_broker, _proposal, _snapshot):
            raise RuntimeError("adapter exploded")

        runner = CapabilityRunner(
            actions=self.broker,
            state=self.state,
            evidence=self.evidence,
            clock=self.clock,
            sleeper=self.clock.sleep,
            step_hook=explode,
        )
        operation = runner.start(
            "Testmage",
            "item.audit",
            {"item_id": "100", "methods": ["look"]},
            timeout_seconds=10,
        )
        terminal = self.wait_terminal(runner, operation.operation_id)
        self.assertEqual(terminal.status, "failed")
        self.assertIn("adapter exploded", terminal.explanation)

    def test_hunt_prepare_hands_off_to_bigshot_only_after_verified_readiness(self):
        self.configure_hunt("Testmage")
        operation = self.runner.perform("Testmage", "hunt.prepare", {})

        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(self.driver.commands, ["bigshot start"])
        self.assertEqual(operation.end_state.generation, "generation-1")
        self.assertEqual(operation.end_state.owners["movement"], "bigshot")
        self.assertEqual(operation.end_state.owners["combat"], "bigshot")
        self.assertIn("bigshot", operation.end_state.scripts)
        action_results = [
            event for event in self.audit if event["event"] == "action_result"
        ]
        self.assertEqual(action_results[-1]["completion"], "sent_unverified")

    def test_manifest_controller_hunt_verifies_result_and_safe_owner_release(self):
        self.configure_controller_hunt()

        operation = self.runner.perform(
            "Testknight",
            "controller.hunt",
            {"profile": "test-hunt"},
        )

        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(
            self.driver.commands, ["lab-test-hunt start test-hunt"]
        )
        self.assertEqual(operation.evidence[-1].method, "controller.hunt")
        self.assertEqual(operation.evidence[-1].facts["code"], "returned")
        self.assertEqual(operation.end_state.room_id, "1000")
        self.assertIsNone(operation.end_state.owners["movement"])
        self.assertIsNone(operation.end_state.owners["combat"])
        results = [event for event in self.audit if event["event"] == "action_result"]
        self.assertEqual(results[-1]["completion"], "sent_unverified")

    def test_manifest_controller_hunt_fails_closed_without_result_or_known_profile(self):
        self.configure_controller_hunt()
        self.evidence.controller_result = None
        missing = self.runner.perform(
            "Testknight",
            "controller.hunt",
            {"profile": "test-hunt"},
        )
        self.assertEqual(missing.status, "failed")
        self.assertIn("result evidence", missing.explanation)

        self.driver.commands.clear()
        unknown = self.runner.perform(
            "Testknight",
            "controller.hunt",
            {"profile": "not-registered"},
        )
        self.assertEqual(unknown.status, "failed")
        self.assertIn("must be one of", unknown.explanation)
        self.assertEqual(self.driver.commands, [])

    def test_hunt_prepare_refuses_unknown_or_unsafe_readiness(self):
        cases = (
            ("dead", None, "known alive"),
            ("stunned", True, "known unstunned"),
            ("wounds", {"head": {"wound": 2}}, "severe wounds"),
            ("hands", {"right": None, "left": None}, "is not in hand"),
            ("fresh", False, "fresh structured"),
            ("encumbrance", 5, "encumbrance below the configured"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                self.configure_hunt("Testmage")
                setattr(self.state, field, value)
                before = len(self.driver.commands)
                operation = self.runner.perform("Testmage", "hunt.prepare", {})
                self.assertEqual(operation.status, "failed")
                self.assertIn(message, operation.explanation)
                self.assertEqual(len(self.driver.commands), before)

    def test_hunt_prepare_refuses_conflicting_lane_owner(self):
        self.configure_hunt("Testscout")
        self.state.owners["movement"] = "go2"
        operation = self.runner.perform("Testscout", "hunt.prepare", {})
        self.assertEqual(operation.status, "failed")
        self.assertIn("ownership conflict in movement", operation.explanation)
        self.assertEqual(self.driver.commands, [])

    def test_hunt_prepare_fails_on_stale_post_generation(self):
        self.configure_hunt("Testmage")
        self.driver.after_command = lambda command: setattr(
            self.state, "generation", "generation-2"
        ) if command == "bigshot start" else None
        operation = self.runner.perform("Testmage", "hunt.prepare", {})
        self.assertEqual(operation.status, "failed")
        self.assertIn("stale session generation", operation.explanation)
        self.assertEqual(self.driver.commands, ["bigshot start"])

    def test_hunt_prepare_sent_without_new_verified_state_is_not_success(self):
        self.configure_hunt("Testmage")
        self.driver.skip_state_update_for.add("bigshot start")
        operation = self.runner.perform("Testmage", "hunt.prepare", {})
        self.assertEqual(operation.status, "failed")
        self.assertIn("post-state did not advance", operation.explanation)
        self.assertEqual(self.driver.commands, ["bigshot start"])

    def test_room_loot_verifies_eloot_finished_and_exact_corpses_are_gone(self):
        self.configure_room_loot()
        operation = self.runner.perform("Testmage", "room.loot", {})

        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(self.driver.commands, ["eloot loot"])
        self.assertEqual(operation.end_state.nearby_corpses, ())
        self.assertTrue(
            operation.end_state.script_status["eloot"].startswith("completed:")
        )
        self.assertNotIn("eloot", operation.end_state.scripts)

    def test_room_loot_refuses_live_creatures_and_owner_conflict(self):
        self.configure_room_loot()
        self.state.nearby_creatures = (
            NearbyObject(object_id="creature-1", noun="mastiff"),
        )
        creatures = self.runner.perform("Testmage", "room.loot", {})
        self.assertEqual(creatures.status, "failed")
        self.assertIn("live creatures", creatures.explanation)
        self.assertEqual(self.driver.commands, [])

        self.configure_room_loot()
        self.state.owners["inventory"] = "eherbs"
        owner = self.runner.perform("Testmage", "room.loot", {})
        self.assertEqual(owner.status, "failed")
        self.assertIn("ownership conflict in inventory", owner.explanation)
        self.assertEqual(self.driver.commands, [])

    def test_room_loot_sent_without_verified_state_is_not_success(self):
        self.configure_room_loot()
        self.driver.skip_state_update_for.add("eloot loot")
        operation = self.runner.perform("Testmage", "room.loot", {})
        self.assertEqual(operation.status, "failed")
        self.assertIn("post-state did not advance", operation.explanation)
        self.assertEqual(self.driver.commands, ["eloot loot"])

    def test_room_loot_fails_if_any_starting_exact_corpse_remains(self):
        self.configure_room_loot()
        self.driver.retain_corpse_ids.add("corpse-2")
        operation = self.runner.perform("Testmage", "room.loot", {})
        self.assertEqual(operation.status, "failed")
        self.assertIn("corpse-2", operation.explanation)

    def test_room_loot_requires_a_clean_script_execution_baseline(self):
        self.configure_room_loot()
        self.state.script_status = {"eloot": "running"}
        operation = self.runner.perform("Testmage", "room.loot", {})
        self.assertEqual(operation.status, "failed")
        self.assertIn("clean ELoot execution baseline", operation.explanation)
        self.assertEqual(self.driver.commands, [])

    def test_character_pilot_lease_rejects_second_nonterminal_operation(self):
        runner, driver = self.blocking_runner()
        first = runner.start(
            "Testmage",
            "item.audit",
            {"item_id": "100", "methods": ["look"]},
            timeout_seconds=10,
        )
        self.assertTrue(driver.entered.wait(1))
        proposed_before = len(
            [event for event in self.audit if event["event"] == "action_proposed"]
        )
        try:
            with self.assertRaisesRegex(ValidationError, "already owns"):
                runner.start(
                    "testmage",
                    "item.audit",
                    {"item_id": "100", "methods": ["look"]},
                )
            with self.assertRaisesRegex(ValidationError, "already owns"):
                runner.perform(
                    "TESTMAGE",
                    "item.audit",
                    {"item_id": "100", "methods": ["look"]},
                )
            proposed_after = len(
                [
                    event
                    for event in self.audit
                    if event["event"] == "action_proposed"
                ]
            )
            self.assertEqual(proposed_after, proposed_before)

            other = runner.start("Testscout", "unsupported.capability", {})
            self.assertEqual(
                self.wait_terminal(runner, other.operation_id).status, "failed"
            )
        finally:
            driver.release.set()
        self.wait_terminal(runner, first.operation_id)


if __name__ == "__main__":
    unittest.main()
