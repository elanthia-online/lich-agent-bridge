import unittest
from datetime import datetime, timezone

from lich_agent_bridge.actions import (
    ActionBroker,
    ActionContext,
    ActionControl,
    ActionResult,
)
from lich_agent_bridge.errors import ValidationError
from lich_agent_bridge.inventory import InventoryItem
from lich_agent_bridge.knowledge import KnowledgeExcerpt
from lich_agent_bridge.operations import CapabilityRunner, ItemBinding
from lich_agent_bridge.protocol import CharacterSnapshot, MeaningfulEvent
from lich_agent_bridge.session_hub import (
    SessionHub,
    WorldStateEvidenceAdapter,
    WorldStateOperationAdapter,
)
from lich_agent_bridge.world_state import WorldState


NOW = datetime(2026, 8, 31, 12, 0, 5, tzinfo=timezone.utc)


def inventory_item(
    dossier_id="dossier-1", fingerprint="fingerprint-1", object_id="100"
):
    return InventoryItem(
        dossier_id=dossier_id,
        fingerprint=fingerprint,
        item_type="weapon",
        noun="runestaff",
        name="training staff",
        full_name="an training staff",
        last_game_id=object_id,
        last_seen_at="2026-08-31T12:00:00Z",
        last_location={
            "kind": "container",
            "game_id": object_id,
            "observed_at": "2026-08-31T11:00:00Z",
            "authority": "last_observation_only",
        },
        facts=(),
    )


class FakeInventory:
    def __init__(self, items=()):
        self.items = tuple(items)
        self.calls = []

    def find(self, *, character, query, limit=20):
        self.calls.append((character, query, limit))
        if query.startswith("#"):
            object_id = query.removeprefix("#")
            return tuple(item for item in self.items if item.last_game_id == object_id)
        return self.items[:limit]


class FakeKnowledge:
    def __init__(self):
        self.calls = []

    def search(self, *, character, question):
        self.calls.append((character, question))
        return (
            KnowledgeExcerpt(
                authority="curated project knowledge",
                title="Ensorcell",
                text="A bounded excerpt.",
                source="wiki/gsiv/Ensorcell.md",
            ),
        )


def publish_snapshot(
    world,
    *,
    generation="generation-1",
    sequence=1,
    item_id="100",
    item_name="an training staff",
):
    return world.publish_snapshot(
        CharacterSnapshot.from_mapping(
            {
                "character": "Testmage",
                "generation": generation,
                "sequence": sequence,
                "observed_at": "2026-08-31T12:00:00Z",
                "room": {"id": "1234", "title": "Town Well"},
                "hands": {
                    "right": {"id": item_id, "name": item_name},
                    "left": None,
                },
            }
        )
    )


def publish_diagnostic(
    world,
    *,
    generation="generation-1",
    method="look",
    item_id="100",
    action_id="action-placeholder",
):
    return world.publish_event(
        MeaningfulEvent.from_mapping(
            {
                "character": "Testmage",
                "generation": generation,
                "observed_at": "2026-08-31T12:00:01Z",
                "kind": "item_diagnostic",
                "summary": f"Verified {method}.",
                "data": {
                    "method": method,
                    "object_id": item_id,
                    "facts": {"observed": True},
                    "action_id": action_id,
                    "success": True,
                },
            }
        )
    )


def publish_controller_result(
    world,
    *,
    controller="hunt",
    action_id="action-placeholder",
    ok=True,
):
    return world.publish_event(
        MeaningfulEvent.from_mapping(
            {
                "character": "Testmage",
                "generation": "generation-1",
                "observed_at": "2026-08-31T12:00:02Z",
                "kind": "controller_result",
                "summary": f"{controller} finished",
                "data": {
                    "controller": controller,
                    "script": f"lab-{controller}",
                    "action_id": action_id,
                    "ok": ok,
                    "code": "returned" if ok else "unsafe_handoff",
                    "message": "safe handoff complete" if ok else "handoff failed",
                    "details": {"room_id": 8667},
                },
            }
        )
    )


def publish_controller_recovery(
    world,
    *,
    action_id="0123456789abcdef",
    controller="hunt",
    generation="generation-2",
):
    return world.publish_event(
        MeaningfulEvent.from_mapping(
            {
                "character": "Testmage",
                "generation": generation,
                "observed_at": "2026-08-31T12:00:03Z",
                "kind": "controller_recovery",
                "summary": "Synthetic operator recovery",
                "data": {
                    "controller": controller,
                    "action_id": action_id,
                    "previous_generation": "generation-1",
                    "operator_confirmed": True,
                    "room_id": "1000",
                    "hands": {"left": None, "right": "777"},
                },
            }
        )
    )


class BrokerDriver:
    def __init__(self, world, *, mismatch=False, replace_generation=False):
        self.world = world
        self.mismatch = mismatch
        self.replace_generation = replace_generation
        self.commands = []

    def __call__(self, broker, proposal, snapshot):
        self.commands.append(proposal["command"])
        action = broker.poll(
            ActionContext(character=snapshot.character, room_id=snapshot.room_id)
        )
        if self.replace_generation:
            publish_snapshot(self.world, generation="generation-2", sequence=1)
        else:
            method = "inspect" if self.mismatch else "look"
            publish_diagnostic(
                self.world,
                method=method,
                action_id=str(action["action_id"]),
            )
        broker.record_result(
            ActionResult(
                action_id=action["action_id"],
                character=snapshot.character,
                outcome="completed",
                detail="command sent",
            )
        )


class SessionHubTests(unittest.TestCase):
    def setUp(self):
        self.world = WorldState(now=lambda: NOW)
        publish_snapshot(self.world)
        self.inventory = FakeInventory((inventory_item(),))
        self.knowledge = FakeKnowledge()
        self.audit = []
        self.actions = ActionBroker(audit=self.audit.append)
        self.actions.admit_generation("Testmage", "generation-1")
        self.actions.control(ActionControl(character="Testmage", enabled=True))

    def hub(self, *, inventory=None, driver=None, timing=None):
        selected_inventory = inventory or self.inventory
        state = WorldStateOperationAdapter(self.world, selected_inventory)
        evidence = WorldStateEvidenceAdapter(self.world)
        runner = CapabilityRunner(
            actions=self.actions,
            state=state,
            evidence=evidence,
            step_hook=driver,
            action_poll_interval=0.001,
        )
        return SessionHub(
            world_state=self.world,
            actions=self.actions,
            inventory=selected_inventory,
            knowledge=self.knowledge,
            capabilities=runner,
            timing=timing,
        )

    def test_snapshot_records_read_latency_and_observed_state_age(self):
        recorded = []
        ticks = iter((20.0, 20.004))
        hub = self.hub(timing=recorded.append)
        hub._monotonic = lambda: next(ticks)

        hub.snapshot({"character": "Testmage"})

        metrics = {record["metric"]: record for record in recorded}
        self.assertAlmostEqual(metrics["snapshot.read_ms"]["value_ms"], 4.0)
        self.assertEqual(metrics["snapshot.age_ms"]["value_ms"], 5000.0)
        self.assertEqual(metrics["snapshot.age_ms"]["character"], "Testmage")

    def test_snapshot_and_watch_use_compact_cursor_envelopes(self):
        hub = self.hub()
        current = hub.snapshot({"character": "Testmage"})
        self.assertEqual(current["room"]["id"], "1234")
        self.assertIsInstance(current["cursor"], str)

        page = hub.watch(
            {"character": "Testmage", "cursor": "0", "timeout_ms": 0}
        )
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["kind"], "snapshot")
        self.assertIsInstance(page["cursor"], str)

    def test_controller_evidence_requires_generation_controller_and_action_id(self):
        adapter = WorldStateEvidenceAdapter(self.world)
        registration = adapter.register_controller(
            "operation-1", "hunt", "Testmage", "generation-1"
        )
        publish_controller_result(
            self.world,
            controller="room",
            action_id="wrong-action",
        )
        publish_controller_result(
            self.world,
            controller="hunt",
            action_id="correct-action",
        )

        evidence = adapter.verify_controller(
            registration,
            "correct-action",
            0,
        )

        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.method, "controller.hunt")
        self.assertEqual(evidence.action_id, "correct-action")
        self.assertEqual(evidence.facts["code"], "returned")
        self.assertEqual(evidence.facts["details"], {"room_id": 8667})

    def test_recovery_receipt_reader_is_exact_and_prefers_newest_event(self):
        adapter = WorldStateEvidenceAdapter(self.world)
        publish_snapshot(self.world, generation="generation-2", sequence=1)
        publish_controller_recovery(self.world, controller="old")
        publish_controller_recovery(self.world, controller="quick")
        publish_controller_recovery(
            self.world,
            action_id="ffffffffffffffff",
            controller="wrong-action",
        )

        receipt = adapter.controller_recovery_receipt(
            character="Testmage",
            generation="generation-2",
            action_id="0123456789abcdef",
        )

        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["controller"], "quick")
        self.assertIsNone(
            adapter.controller_recovery_receipt(
                character="Testmage",
                generation="generation-3",
                action_id="0123456789abcdef",
            )
        )

    def test_inventory_and_wiki_return_compact_provenance_envelopes(self):
        hub = self.hub()
        inventory = hub.inventory_find({"character": "Testmage", "query": "staff"})
        self.assertEqual(inventory["total"], 1)
        self.assertEqual(
            inventory["items"][0]["last_location"]["authority"],
            "last_observation_only",
        )
        wiki = hub.wiki_search(
            {"query": "ensorcell", "character": "Testmage", "limit": 1}
        )
        self.assertEqual(wiki["total"], 1)
        self.assertEqual(wiki["items"][0]["source"], "wiki/gsiv/Ensorcell.md")

    def test_capabilities_returns_strict_discoverable_catalog(self):
        hub = self.hub()

        result = hub.capability_catalog({"character": "Testknight"})

        self.assertEqual(result["character"], "Testknight")
        self.assertEqual(result["total"], 6)
        self.assertFalse(any(item["name"].startswith("controller.") for item in result["items"]))
        self.assertEqual(
            {item["name"] for item in result["items"]},
            {
                "travel.go2",
                "character.recon",
                "item.audit",
                "hunt.prepare",
                "room.loot",
                "session.recover_controller",
            },
        )
        with self.assertRaisesRegex(ValidationError, "unsupported capabilities request field"):
            hub.capability_catalog({"extra": True})

    def test_operation_state_binds_unique_dossier_to_exact_live_hand(self):
        state = WorldStateOperationAdapter(self.world, self.inventory).snapshot("Testmage")
        self.assertEqual(len(state.items), 1)
        item = state.items[0]
        self.assertEqual(item.object_id, "100")
        self.assertEqual(item.dossier_id, "dossier-1")
        self.assertEqual(item.location.kind, "right_hand")
        self.assertTrue(item.location.verified_owned)
        self.assertNotEqual(item.location.kind, "container")

    def test_live_hand_item_projects_once_without_a_durable_dossier_match(self):
        adapter = WorldStateOperationAdapter(self.world, FakeInventory())
        state = adapter.snapshot("Testmage")

        self.assertEqual(len(state.items), 1)
        item = state.items[0]
        self.assertEqual(item.object_id, "100")
        self.assertTrue(item.dossier_id)
        self.assertTrue(item.fingerprint)
        self.assertTrue(item.dossier_id.startswith("live-hand:"))
        self.assertTrue(item.fingerprint.startswith("lich-live-hand-v1:"))
        self.assertEqual(item.location.kind, "right_hand")
        self.assertTrue(item.location.verified_owned)
        repeated = adapter.snapshot("Testmage").items[0]
        self.assertEqual(repeated.dossier_id, item.dossier_id)
        self.assertEqual(repeated.fingerprint, item.fingerprint)

    def test_live_hand_fallback_identity_changes_with_generation_and_name(self):
        adapter = WorldStateOperationAdapter(self.world, FakeInventory())
        first = adapter.snapshot("Testmage").items[0]

        publish_snapshot(self.world, generation="generation-2", sequence=1)
        second = adapter.snapshot("Testmage").items[0]
        publish_snapshot(
            self.world,
            generation="generation-2",
            sequence=2,
            item_name="a renamed training staff",
        )
        renamed = adapter.snapshot("Testmage").items[0]

        self.assertNotEqual(second.fingerprint, first.fingerprint)
        self.assertNotEqual(renamed.fingerprint, second.fingerprint)

    def test_operation_state_maps_structured_live_fields(self):
        self.world.publish_snapshot(
            CharacterSnapshot.from_mapping(
                {
                    "character": "Testmage",
                    "generation": "generation-1",
                    "sequence": 2,
                    "observed_at": "2026-08-31T12:00:01Z",
                    "room": {"id": "1234"},
                    "dead": False,
                    "stunned": False,
                    "wounds": {},
                    "encumbrance": 0,
                    "hands": {
                        "right": {"id": "100", "name": "an training staff"},
                        "left": None,
                    },
                    "nearby": {
                        "creatures": [],
                        "corpses": [
                            {"id": "500", "noun": "mastiff", "name": "a mastiff"}
                        ],
                    },
                    "scripts": ["eloot"],
                    "owners": {"inventory": "eloot"},
                    "script_status": {"eloot": "running"},
                }
            )
        )
        state = WorldStateOperationAdapter(self.world, self.inventory).snapshot("Testmage")
        self.assertTrue(state.fresh)
        self.assertEqual(state.sequence, 2)
        self.assertEqual(state.hands["right"].object_id, "100")
        self.assertEqual(state.nearby_corpses[0].object_id, "500")
        self.assertEqual(state.owners["inventory"], "eloot")
        self.assertEqual(state.script_status["eloot"], "running")

    def test_matching_explicit_evidence_succeeds_through_broker(self):
        driver = BrokerDriver(self.world)
        result = self.hub(driver=driver).perform(
            {
                "character": "Testmage",
                "capability": "item.audit",
                "args": {"item_id": "100", "methods": ["look"]},
            }
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(driver.commands, ["look #100"])
        self.assertEqual(result["evidence"][0]["method"], "look")
        self.assertTrue(result["progress"])
        self.assertTrue(any(item["event"] == "action_proposed" for item in self.audit))

    def test_live_hand_without_dossier_reaches_broker_for_item_audit(self):
        driver = BrokerDriver(self.world)
        result = self.hub(inventory=FakeInventory(), driver=driver).perform(
            {
                "character": "Testmage",
                "capability": "item.audit",
                "args": {"item_id": "100", "methods": ["look"]},
            }
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(driver.commands, ["look #100"])

    def test_mismatched_evidence_is_rejected(self):
        result = self.hub(driver=BrokerDriver(self.world, mismatch=True)).perform(
            {
                "character": "Testmage",
                "capability": "item.audit",
                "args": {"item_id": "100", "methods": ["look"]},
            }
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("registered look evidence was not observed", result["explanation"])

    def test_generation_replacement_fails_running_operation(self):
        result = self.hub(
            driver=BrokerDriver(self.world, replace_generation=True)
        ).perform(
            {
                "character": "Testmage",
                "capability": "item.audit",
                "args": {"item_id": "100", "methods": ["look"]},
            }
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("stale session generation", result["explanation"])

    def test_nonhand_ambiguous_and_unsupported_operations_fail_without_action(self):
        ambiguous = FakeInventory(
            (
                inventory_item(),
                inventory_item("dossier-2", "fingerprint-2"),
            )
        )
        cases = [
            (
                self.hub(),
                {"item_id": "999", "methods": ["look"]},
                "ambiguous item identity",
            ),
            (
                self.hub(inventory=ambiguous),
                {"item_id": "100", "methods": ["look"]},
                "ambiguous item identity",
            ),
        ]
        for hub, arguments, message in cases:
            with self.subTest(arguments=arguments):
                result = hub.perform(
                    {
                        "character": "Testmage",
                        "capability": "item.audit",
                        "args": arguments,
                    }
                )
                self.assertEqual(result["status"], "failed")
                self.assertIn(message, result["explanation"])
        unsupported = self.hub().perform(
            {"character": "Testmage", "capability": "room.loot"}
        )
        self.assertEqual(unsupported["status"], "failed")
        self.assertIn("known nearby creature state", unsupported["explanation"])

    def test_evidence_adapter_ignores_wrong_method_and_object(self):
        state = WorldStateOperationAdapter(self.world, self.inventory).snapshot("Testmage")
        item = state.items[0]
        binding = ItemBinding(
            character=state.character,
            generation=state.generation,
            object_id=item.object_id,
            dossier_id=item.dossier_id,
            fingerprint=item.fingerprint,
            original_location=item.location,
        )
        adapter = WorldStateEvidenceAdapter(self.world)
        publish_diagnostic(self.world, method="look")
        registration = adapter.register("operation-1", "look", binding)
        self.assertIsNone(adapter.verify(registration))
        publish_diagnostic(self.world, method="inspect")
        publish_diagnostic(self.world, method="look", item_id="999")
        self.assertIsNone(adapter.verify(registration))

    def test_evidence_adapter_requires_exact_action_id_and_success(self):
        state = WorldStateOperationAdapter(self.world, self.inventory).snapshot("Testmage")
        item = state.items[0]
        binding = ItemBinding(
            character=state.character,
            generation=state.generation,
            object_id=item.object_id,
            dossier_id=item.dossier_id,
            fingerprint=item.fingerprint,
            original_location=item.location,
        )
        adapter = WorldStateEvidenceAdapter(self.world)
        registration = adapter.register("operation-1", "look", binding)
        publish_diagnostic(self.world, action_id="wrong-action")
        self.assertIsNone(adapter.verify(registration, "expected-action"))
        publish_diagnostic(self.world, action_id="expected-action")
        evidence = adapter.verify(registration, "expected-action")
        self.assertEqual(evidence.action_id, "expected-action")


if __name__ == "__main__":
    unittest.main()
