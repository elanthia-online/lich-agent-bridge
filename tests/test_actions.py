import tempfile
import threading
import time
import unittest
from pathlib import Path

from lich_agent_bridge.actions import (
    ActionApproval,
    ActionBroker,
    ActionContext,
    ActionControl,
    ActionNextRequest,
    ActionProposal,
    ActionResult,
    CommandPolicy,
    load_or_create_action_token,
)
from lich_agent_bridge.errors import ConfigurationError, ValidationError
from lich_agent_bridge.controller_manifest import ControllerManifest


class FakeClock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


class ActionBrokerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.audit = []
        self.broker = ActionBroker(audit=self.audit.append, clock=self.clock,
                                   policy=CommandPolicy(ControllerManifest.load(Path(__file__).parent / "fixtures" / "controllers.json")))
        self.broker.admit_generation("Testscout", "generation-1")
        self.broker.control(ActionControl(character="Testscout", enabled=True))

    def test_cancel_pending_action_by_exact_identity_leaves_other_actions_alone(self):
        for commands in (("info",), ("info", "skills")):
            with self.subTest(commands=commands):
                proposal = self.broker.submit(ActionProposal(
                    character="Testscout", command=commands[0] if len(commands) == 1 else None,
                    commands=commands if len(commands) > 1 else (),
                ))
                unrelated = self.broker.submit(ActionProposal(character="Testscout", command="look"))
                cancelled = self.broker.cancel(proposal["action_id"], character="testscout", generation="generation-1")
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertTrue(cancelled["cancelled"])
                with self.assertRaises(ValidationError):
                    self.broker.approve(ActionApproval(proposal["action_id"], "Testscout", "1000"))
                self.assertEqual(self.broker.get(unrelated["action_id"])["status"], "queued")
                self.broker.cancel(unrelated["action_id"], character="Testscout", generation="generation-1")
                self.assertIsNone(self.broker.poll(ActionContext("Testscout", "1000")))

    def test_cancel_rejects_other_character_or_generation(self):
        proposal = self.broker.submit(ActionProposal(character="Testscout", command="info"))
        for character, generation in (("Testmage", "generation-1"), ("Testscout", "generation-2")):
            with self.assertRaises(ValidationError):
                self.broker.cancel(proposal["action_id"], character=character, generation=generation)
            self.assertEqual(self.broker.get(proposal["action_id"])["status"], "queued")

    def test_cancel_cannot_unsend_dispatched_action(self):
        proposal = self.broker.submit(ActionProposal(character="Testscout", command="info"))
        self.broker.poll(ActionContext("Testscout", "1000"))
        result = self.broker.cancel(proposal["action_id"], character="Testscout", generation="generation-1")
        self.assertEqual(result["status"], "dispatched")
        self.assertFalse(result["cancelled"])
        self.assertEqual(self.broker.get(proposal["action_id"])["status"], "dispatched")

    def test_inspection_dispatches_once_and_records_result(self):
        proposed = self.broker.submit(
            ActionProposal(character="Testscout", command="READY   LIST")
        )
        self.assertEqual(proposed["status"], "queued")

        action = self.broker.poll(ActionContext(character="testscout", room_id="1000"))
        self.assertEqual(action["status"], "execute")
        self.assertEqual(action["command"], "ready list")
        self.assertIsNone(
            self.broker.poll(ActionContext(character="Testscout", room_id="1000"))
        )

        result = self.broker.record_result(
            ActionResult(
                action_id=action["action_id"],
                character="Testscout",
                outcome="completed",
                detail="command sent",
            )
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completion"], "sent_unverified")
        self.assertEqual(
            self.broker.get(action["action_id"])["completion"], "sent_unverified"
        )

    def test_full_access_wrapper_dispatches_one_audited_game_command(self):
        proposed = self.broker.submit(
            ActionProposal(character="Testscout", command="lab direct join Testleader")
        )

        self.assertEqual(proposed["status"], "queued")
        self.assertEqual(proposed["kind"], "direct")
        action = self.broker.poll(ActionContext("Testscout", "1000"))
        self.assertEqual(action["command"], "lab direct join Testleader")

    def test_full_access_wrapper_dispatches_one_lich_script_command(self):
        proposed = self.broker.submit(
            ActionProposal(
                character="Testscout",
                command="lab direct ;eohunter Leveling-Trio dry",
            )
        )

        self.assertEqual(proposed["status"], "queued")
        self.assertEqual(proposed["kind"], "direct")
        action = self.broker.poll(ActionContext("Testscout", "1000"))
        self.assertEqual(
            action["command"], "lab direct ;eohunter Leveling-Trio dry"
        )

    def test_full_access_wrapper_rejects_client_commands_and_chaining(self):
        for command in (
            "lab direct ;e puts 'no'",
            "lab direct ;exec puts 'no'",
            "lab direct , ask something",
            "lab direct ;eohunter Leveling-Trio dry;quit",
            "lab direct join Testleader;drop all",
            "lab direct join Testleader\nquit",
        ):
            with self.subTest(command=command):
                with self.assertRaises(ValidationError):
                    self.broker.submit(
                        ActionProposal(character="Testscout", command=command)
                    )

    def test_poll_wait_wakes_when_an_action_is_submitted(self):
        observed = []

        thread = threading.Thread(
            target=lambda: observed.append(
                self.broker.poll_wait(
                    ActionContext(character="Testscout", room_id="1000"),
                    timeout_seconds=1,
                )
            )
        )
        thread.start()
        time.sleep(0.02)
        self.broker.submit(ActionProposal(character="Testscout", command="look"))
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(observed[0]["status"], "execute")
        self.assertEqual(observed[0]["command"], "look")

    def test_poll_wait_returns_none_after_bounded_timeout(self):
        started = time.monotonic()
        result = self.broker.poll_wait(
            ActionContext(character="Testscout", room_id="1000"), timeout_seconds=0.02
        )

        self.assertIsNone(result)
        self.assertGreaterEqual(time.monotonic() - started, 0.015)

    def test_inventory_and_crafting_sequence_is_one_explicit_confirmed_action(self):
        proposed = self.broker.submit(
            ActionProposal(
                character="Testscout",
                commands=(
                    "PUT   my runestone in my robes",
                    "get my smooth stone",
                    "wave my stone at my scroll",
                    "put my stone in my satchel",
                    "get my runestone",
                ),
                expected_room_id="1000",
            )
        )

        self.assertEqual(proposed["status"], "confirmation_required")
        self.assertEqual(proposed["kind"], "sequence")
        self.assertEqual(proposed["step_count"], 5)
        self.assertEqual(
            proposed["commands"],
            [
                "PUT my runestone in my robes",
                "get my smooth stone",
                "wave my stone at my scroll",
                "put my stone in my satchel",
                "get my runestone",
            ],
        )
        self.assertNotIn("command", proposed)

        notice = self.broker.poll(
            ActionContext(character="Testscout", room_id="1000")
        )
        self.assertEqual(notice["commands"], proposed["commands"])
        self.broker.approve(
            ActionApproval(
                action_id=proposed["action_id"],
                character="Testscout",
                room_id="1000",
                approval_mode="auto",
            )
        )
        dispatched = self.broker.poll(
            ActionContext(character="Testscout", room_id="1000")
        )
        self.assertEqual(dispatched["status"], "execute")
        self.assertEqual(dispatched["commands"], proposed["commands"])
        self.assertIsNone(
            self.broker.poll(ActionContext(character="Testscout", room_id="1000"))
        )

        proposed_audit = next(
            event for event in self.audit if event["event"] == "action_proposed"
        )
        self.assertEqual(proposed_audit["commands"], proposed["commands"])
        self.assertEqual(proposed_audit["step_count"], 5)

    def test_sequence_payload_requires_two_to_twelve_commands_and_no_command_field(self):
        with self.assertRaisesRegex(ValidationError, "exactly one"):
            ActionProposal.from_mapping(
                {
                    "character": "Testscout",
                    "command": "look",
                    "commands": ["look", "inventory"],
                }
            )
        with self.assertRaisesRegex(ValidationError, "between 2 and 12"):
            ActionProposal.from_mapping(
                {"character": "Testscout", "commands": ["look"]}
            )
        with self.assertRaisesRegex(ValidationError, "between 2 and 12"):
            ActionProposal.from_mapping(
                {"character": "Testscout", "commands": ["look"] * 13}
            )

    def test_sequence_rejects_movement_combat_communication_and_destructive_steps(self):
        for commands in (
            ("get my stone", "north"),
            ("look", "attack troll"),
            ("look", "say hello"),
            ("prepare 714", "cast at my scroll"),
            ("get my stone", "drop my stone"),
        ):
            with self.subTest(commands=commands):
                with self.assertRaises(ValidationError):
                    self.broker.submit(
                        ActionProposal(character="Testscout", commands=commands)
                    )

    def test_sequence_accepts_narrow_exact_id_scroll_infusion_cast(self):
        proposed = self.broker.submit(
            ActionProposal(
                character="Testscout",
                commands=(
                    "prepare 714",
                    "cast at #15028257",
                    "infuse #15028257",
                ),
            )
        )

        self.assertEqual(proposed["kind"], "sequence")
        self.assertEqual(proposed["status"], "confirmation_required")

    def test_poll_wait_rejects_invalid_timeout(self):
        with self.assertRaisesRegex(ValidationError, "between 0 and 30"):
            self.broker.poll_wait(
                ActionContext(character="Testscout", room_id="1000"),
                timeout_seconds=31,
            )

    def test_action_next_request_is_strict_and_bounded(self):
        parsed = ActionNextRequest.from_mapping(
            {
                "character": "Testscout",
                "room_id": 1000,
                "generation": "generation-1",
                "timeout_seconds": 2.5,
            }
        )
        self.assertEqual(parsed.context.room_id, "1000")
        self.assertEqual(parsed.timeout_seconds, 2.5)
        with self.assertRaisesRegex(ValidationError, "unsupported field"):
                ActionNextRequest.from_mapping(
                {
                    "character": "Testscout",
                    "room_id": "1000",
                    "generation": "generation-1",
                    "extra": True,
                }
            )

    def test_explicit_evidence_verified_result_is_distinguished(self):
        proposed = self.broker.submit(
            ActionProposal(character="Testscout", command="look")
        )
        action = self.broker.poll(ActionContext(character="Testscout", room_id="1000"))
        result = self.broker.record_result(
            ActionResult(
                action_id=proposed["action_id"],
                character="Testscout",
                outcome="succeeded",
                detail="registered matcher observed the expected response",
            )
        )
        self.assertEqual(action["status"], "execute")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completion"], "succeeded")

    def test_configuration_requires_exact_confirmation_and_room_stays_fresh(self):
        proposed = self.broker.submit(
            ActionProposal(character="Testscout", command="ready weapon clear")
        )
        notice = self.broker.poll(ActionContext(character="Testscout", room_id="1000"))
        self.assertEqual(notice["status"], "confirmation_required")
        self.assertIsNone(
            self.broker.poll(ActionContext(character="Testscout", room_id="1000"))
        )

        approved = self.broker.approve(
            ActionApproval(
                action_id=proposed["action_id"],
                character="Testscout",
                room_id="1000",
            )
        )
        self.assertEqual(approved["expected_room_id"], "1000")
        self.assertIsNone(
            self.broker.poll(ActionContext(character="Testscout", room_id="9999"))
        )
        with self.assertRaisesRegex(ValidationError, "not dispatched"):
            self.broker.record_result(
                ActionResult(
                    action_id=proposed["action_id"],
                    character="Testscout",
                    outcome="completed",
                    detail="should not happen",
                )
            )

    def test_auto_approval_mode_is_audited(self):
        proposed = self.broker.submit(
            ActionProposal(character="Testscout", command="north")
        )
        self.broker.approve(
            ActionApproval(
                action_id=proposed["action_id"],
                character="Testscout",
                room_id="1000",
                approval_mode="auto",
            )
        )
        approved = [event for event in self.audit if event["event"] == "action_approved"]
        self.assertEqual(approved[-1]["approval_mode"], "auto")

    def test_expired_action_never_dispatches(self):
        proposed = self.broker.submit(
            ActionProposal(character="Testscout", command="inventory", ttl_seconds=2)
        )
        self.clock.now += 2
        self.assertIsNone(
            self.broker.poll(ActionContext(character="Testscout", room_id="1000"))
        )
        self.assertIn(
            proposed["action_id"],
            [event.get("action_id") for event in self.audit if event["event"] == "action_expired"],
        )

    def test_action_never_crosses_character(self):
        self.broker.admit_generation("Testmage", "generation-1")
        self.broker.control(ActionControl(character="Testmage", enabled=True))
        proposed = self.broker.submit(
            ActionProposal(character="Testmage", command="inventory")
        )
        self.assertIsNone(
            self.broker.poll(ActionContext(character="Testscout", room_id="1000"))
        )
        action = self.broker.poll(ActionContext(character="testmage", room_id="100"))
        self.assertEqual(action["action_id"], proposed["action_id"])

    def test_kill_switch_cancels_pending_actions(self):
        self.broker.submit(ActionProposal(character="Testscout", command="inventory"))
        self.broker.control(ActionControl(character="Testscout", enabled=False))
        self.assertIsNone(
            self.broker.poll(ActionContext(character="Testscout", room_id="1000"))
        )
        with self.assertRaisesRegex(ValidationError, "disabled"):
            self.broker.submit(ActionProposal(character="Testscout", command="health"))

    def test_generation_replacement_fences_a_stale_bridge_and_cancels_pending(self):
        proposed = self.broker.submit(
            ActionProposal(character="Testscout", command="north")
        )
        self.assertEqual(proposed["generation"], "generation-1")

        replacement = self.broker.admit_generation("Testscout", "generation-2")
        self.assertEqual(replacement["cancelled_action_ids"], [proposed["action_id"]])
        self.assertEqual(self.broker.get(proposed["action_id"])["status"], "cancelled")
        with self.assertRaisesRegex(ValidationError, "does not match active bridge"):
            self.broker.poll(
                ActionContext(
                    character="Testscout", room_id="1000", generation="generation-1"
                )
            )
        with self.assertRaisesRegex(ValidationError, "does not match active bridge"):
            self.broker.control(
                ActionControl(
                    character="Testscout", enabled=True, generation="generation-1"
                )
            )

        self.broker.control(
            ActionControl(character="Testscout", enabled=True, generation="generation-2")
        )
        with self.assertRaisesRegex(ValidationError, "proposal generation"):
            self.broker.submit(
                ActionProposal(
                    character="Testscout",
                    command="look",
                    expected_generation="generation-1",
                )
            )
        fresh = self.broker.submit(ActionProposal(character="Testscout", command="look"))
        delivered = self.broker.poll(
            ActionContext(
                character="Testscout", room_id="1000", generation="generation-2"
            )
        )
        self.assertEqual(delivered["action_id"], fresh["action_id"])
        self.assertEqual(delivered["generation"], "generation-2")

    def test_policy_denies_chained_and_destructive_commands(self):
        for command in (
            "inventory;drop maul",
            "drop maul",
            "sell my maul",
            "give maul to Bob",
            "mark maul remove",
            "set nomarkeddrop off",
        ):
            with self.subTest(command=command):
                with self.assertRaises(ValidationError):
                    self.broker.submit(
                        ActionProposal(character="Testscout", command=command)
                    )

    def test_ordinary_driver_commands_are_confirmation_gated(self):
        for command, kind in (
            ("get my maul", "inventory"),
            ("put my maul in my harness", "inventory"),
            ("attack troll", "combat"),
            ("buy ticket", "commerce"),
            ("mark my maul", "configuration"),
            ("register my maul", "configuration"),
            ("set nomarkeddrop on", "configuration"),
            ("go2 getsilvers on", "configuration"),
            ("eloot keep closed on", "configuration"),
            ("eloot loot", "inventory"),
            ("eloot sell", "configuration"),
            ("eloot stop", "configuration"),
            ("eherbs heal", "configuration"),
            ("bigshot start", "configuration"),
            ("bigshot stop", "configuration"),
            ("ebounty start", "configuration"),
            ("ebounty stop", "configuration"),
            ("pour my potion on my stone", "crafting"),
            ("dip my brush in my ceramic ink", "crafting"),
            ("dip my brush in my cup", "crafting"),
            ("draw odeir'cos rune", "crafting"),
            ("wave my runestone at my scroll", "crafting"),
            ("wave #16506792 at #15028257", "crafting"),
            (
                "wave my polished obsidian runestone at my aged parchment",
                "crafting",
            ),
            ("prepare 714", "crafting"),
            ("cast at #15028257", "crafting"),
            ("infuse my scroll", "crafting"),
            ("infuse #15028257", "crafting"),
            ("infuse my sheet of vellum", "crafting"),
            ("lab-test-engage test-living #12345", "combat"),
            ("lab-test-engage test-living #12345 loot", "combat"),
            ("lab-test-engage stop", "configuration"),
            ("lab-test-room start test-living", "combat"),
            ("lab-test-room stop", "configuration"),
            ("lab-test-hunt start test-hunt", "combat"),
            ("lab-test-hunt start test-probe", "combat"),
            ("lab-test-hunt return", "configuration"),
            ("lab-test-rift extract", "movement"),
            ("lab-test-rift drill", "movement"),
            ("lab-test-rift probe", "movement"),
            ("lab-test-rift patrol", "movement"),
            ("lab-test-rift hunt", "movement"),
            ("beseech teleport", "combat"),
        ):
            with self.subTest(command=command):
                proposed = self.broker.submit(
                    ActionProposal(character="Testscout", command=command)
                )
                self.assertEqual(proposed["status"], "confirmation_required")
                self.assertEqual(proposed["kind"], kind)

    def test_lab_hunt_status_is_immediate_inspection(self):
        proposed = self.broker.submit(
            ActionProposal(character="Testscout", command="lab-test-hunt status")
        )
        self.assertEqual(proposed["status"], "queued")
        self.assertEqual(proposed["kind"], "inspection")

    def test_lab_rift_read_operations_are_immediate_inspections(self):
        for command in ("lab-test-rift status", "lab-test-rift preflight"):
            with self.subTest(command=command):
                proposed = self.broker.submit(
                    ActionProposal(character="Testscout", command=command)
                )
                self.assertEqual(proposed["status"], "queued")
                self.assertEqual(proposed["kind"], "inspection")

    def test_movement_and_communication_are_confirmation_gated(self):
        for command in ("north", "go door", "go2 12345", "say open sesame"):
            with self.subTest(command=command):
                proposed = self.broker.submit(
                    ActionProposal(character="Testscout", command=command)
                )
                self.assertEqual(proposed["status"], "confirmation_required")
                expected_kind = "communication" if command.startswith("say ") else "movement"
                self.assertEqual(proposed["kind"], expected_kind)

    def test_communication_preserves_message_case(self):
        proposed = self.broker.submit(
            ActionProposal(character="Testscout", command="say Open Sesame")
        )
        self.assertEqual(proposed["command"], "say Open Sesame")

    def test_exact_local_item_inspection_is_immediate_and_preserves_name(self):
        proposed = self.broker.submit(
            ActionProposal(
                character="Testscout",
                command="inspect item exact Plain Practice Maul",
            )
        )
        self.assertEqual(proposed["status"], "queued")
        self.assertEqual(
            proposed["command"], "inspect item exact Plain Practice Maul"
        )

    def test_exact_item_spell_diagnostics_are_immediate_and_restricted(self):
        self.broker.admit_generation("Testmage", "generation-1")
        self.broker.control(ActionControl(character="Testmage", enabled=True))
        for spell in (405, 735):
            with self.subTest(spell=spell):
                proposed = self.broker.submit(
                    ActionProposal(
                        character="Testmage",
                        command=f"diagnose item {spell} exact Plain Training Staff",
                    )
                )
                self.assertEqual(proposed["status"], "queued")
                self.assertEqual(proposed["kind"], "inspection")
                self.assertEqual(
                    proposed["command"],
                    f"diagnose item {spell} exact Plain Training Staff",
                )

        with self.assertRaises(ValidationError):
            self.broker.submit(
                ActionProposal(
                    character="Testmage",
                    command="diagnose item 720 exact Plain Training Staff",
                )
            )

    def test_ready_link_diagnostic_is_immediate(self):
        proposed = self.broker.submit(
            ActionProposal(character="Testscout", command="inspect ready weapon links")
        )
        self.assertEqual(proposed["status"], "queued")
        self.assertEqual(proposed["kind"], "inspection")

    def test_character_training_diagnostics_are_immediate(self):
        for command in (
            "skills",
            "bounty",
            "cman info",
            "armor info",
            "armor list all",
            "weapon info",
            "shield info",
            "feat info",
            "ascension info",
            "resource",
        ):
            with self.subTest(command=command):
                proposed = self.broker.submit(
                    ActionProposal(character="Testscout", command=command)
                )
                self.assertEqual(proposed["status"], "queued")
                self.assertEqual(proposed["kind"], "inspection")

    def test_unregistered_controller_commands_are_rejected(self):
        for command in ("unregistered-controller status", "unregistered-controller cast 702",
                        "lab-test-engage test-living mastiff", "lab-test-engage test-living #12345 sell",
                        "lab-test-engage other-profile #12345", "lab-test-room start other-profile"):
            with self.subTest(command=command), self.assertRaises(ValidationError):
                self.broker.submit(ActionProposal(character="Testscout", command=command))


class CommandPolicyTests(unittest.TestCase):
    def test_default_policy_does_not_inherit_synthetic_test_controllers(self):
        policy = CommandPolicy(ControllerManifest.load(Path(__file__).parents[1] / "lich" / "lab-controllers.json"))
        with self.assertRaises(ValidationError):
            policy.evaluate("lab-test-hunt start test-hunt")

    def test_scroll_infusion_accepts_lexically_safe_rune_names(self):
        for command in ("draw quiss'fyn rune", "draw ayan'eth rune"):
            with self.subTest(command=command):
                normalized, decision = CommandPolicy().evaluate(command)
                self.assertEqual(normalized, command)
                self.assertEqual(decision.kind, "crafting")
                self.assertTrue(decision.confirmation_required)

    def test_scroll_infusion_rejects_unsafe_rune_names(self):
        for command in (
            "draw unknown rune",
            "draw quiss'fyn2 rune",
            "draw quiss'fyn rune; north",
            "draw quiss'fyn\nrune",
        ):
            with self.subTest(command=command):
                with self.assertRaises(ValidationError):
                    CommandPolicy().evaluate(command)

    def test_shop_protocol_accepts_bare_order_and_buy(self):
        for command in ("order", "buy"):
            with self.subTest(command=command):
                normalized, decision = CommandPolicy().evaluate(command)
                self.assertEqual(normalized, command)
                self.assertEqual(decision.kind, "commerce")
                self.assertTrue(decision.confirmation_required)

    def test_stable_item_id_is_required_for_ready_weapon(self):
        normalized, decision = CommandPolicy().evaluate("ready weapon set #12345")
        self.assertEqual(normalized, "ready weapon set #12345")
        self.assertTrue(decision.confirmation_required)

    def test_hidden_ready_link_syntax_is_allowed(self):
        normalized, decision = CommandPolicy().evaluate("ready weapon #152828719")
        self.assertEqual(normalized, "ready weapon #152828719")
        self.assertTrue(decision.confirmation_required)

    def test_exact_controller_recovery_is_brokered_and_requires_confirmation(self):
        command = "lab recover 0123456789abcdef confirm"
        normalized, decision = CommandPolicy().evaluate(command)
        self.assertEqual(normalized, command)
        self.assertEqual(decision.kind, "configuration")
        self.assertTrue(decision.confirmation_required)

    def test_controller_recovery_rejects_broad_or_malformed_commands(self):
        for command in (
            "lab recover",
            "lab recover all confirm",
            "lab recover 0123456789abcde confirm",
            "lab recover 0123456789abcdef",
            "lab recover 0123456789abcdef confirm extra",
        ):
            with self.subTest(command=command), self.assertRaises(ValidationError):
                CommandPolicy().evaluate(command)


class ActionTokenTests(unittest.TestCase):
    def test_token_is_created_once_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            first = load_or_create_action_token(path)
            second = load_or_create_action_token(path)
            self.assertEqual(first, second)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_public_token_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("a" * 64, encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaises(ConfigurationError):
                load_or_create_action_token(path)


if __name__ == "__main__":
    unittest.main()
