import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from threading import Event, Thread
import unittest
from unittest.mock import Mock

from lich_agent_bridge.actions import ActionApproval, ActionBroker, ActionControl, ActionProposal
from lich_agent_bridge.evidence_tools import EvidenceTools, MAX_EVIDENCE_CHARS
from lich_agent_bridge.errors import QuestionInvalidated, QuestionTimeout, ValidationError
from lich_agent_bridge.question import QuestionControl


def snapshot():
    stamp = datetime.now(timezone.utc).isoformat()
    return {"character": "Testmage", "generation": "one", "game": "GSIV", "observed_at": stamp,
            "freshness": {"stale": False}, "room": {"id": "228"}, "hands": {"right": None, "left": None},
            "vitals": {"health": {"value": 0, "max": 141}}, "dead": False,
            "character_data": {
                "info": {"source": "info", "observed_at": stamp, "complete": True, "observed_level": 90,
                         "values": {"level": 90, "stats": {"STR": {"value": 80, "bonus": 15}}}},
                "skills": {"source": "skills", "observed_at": stamp, "complete": True, "observed_level": 90,
                           "values": {"spell_circles": {"sorcerer": 30}, "skills": {"armoruse": {"ranks": 0}}}}}}


class Hub:
    def __init__(self):
        self.state = snapshot()
        self.capabilities = SimpleNamespace(interrupt=Mock(), start=self.start_operation)
        self.inventory = SimpleNamespace(configured=True)
        self.started = []
        self.queries = []
        self.outcome = "succeeded"
        self.on_watch = None

    def snapshot(self, payload):
        if self.state is None:
            raise ValidationError("no snapshot")
        return copy.deepcopy(self.state)

    def start_operation(self, character, capability, arguments, **options):
        self.started.append({"character": character, "capability": capability,
                             "args": copy.deepcopy(arguments), **options})
        return SimpleNamespace(operation_id="owned-op", status="running", explanation="")

    def watch_operation(self, payload):
        if self.on_watch:
            self.on_watch()
        if self.outcome == "succeeded":
            self.state["character_data"] = snapshot()["character_data"]
        return {"operation": {"operation_id": "owned-op", "status": self.outcome, "explanation": "verified"}, "cursor": "1"}

    def inventory_find(self, payload):
        self.queries.append(payload)
        return {"items": [{"dossier_id": "staff", "last_seen_at": "2026-09-01T00:00:00Z", "facts": [{"field": "tier", "value": 3, "source": "analyze"}]}], "total": 1}

    def wiki_search(self, payload):
        self.queries.append(payload)
        return {"items": [{"source": "local_gswiki", "title": "Tenebrous Tether", "text": "Up to ten seconds.", "revision_id": "234211", "url": "https://gswiki.play.net/Tenebrous_Tether"}], "total": 1, "diagnostics": [{"source": "local_gswiki", "status": "success"}]}


class EvidenceToolsTest(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.control = QuestionControl(3)
        self.session = EvidenceTools(self.hub).open("Testmage", self.control)
        self.addCleanup(self.session.close)

    def execute(self, tool, args=None):
        return self.session.execute(tool, args or {}, self.control)

    def test_catalog_is_copy_and_only_reusable_observation_tools(self):
        catalog = self.session.catalog()
        self.assertEqual({tool["name"] for tool in catalog}, {"state.read", "character.read", "inventory.search", "knowledge.search", "combat.report"})
        catalog[0]["name"] = "commands.run"
        self.assertEqual(self.session.catalog()[0]["name"], "state.read")

    def test_validate_rejects_authority_and_invalid_arguments_without_effects(self):
        for tool, args in [
            ("character.read", {"character": "Testscout"}), ("character.read", {"commands": ["quit"]}),
            ("character.read", {"categories": ["info", "info"]}), ("character.read", {"freshness": []}),
            ("character.read", {"categories": ["wealth"]}), ("state.read", {"sections": []}),
            ("state.read", {"sections": "all"}), ("state.read", {"sections": ["room", 2]}),
            ("inventory.search", {"query": ""}), ("inventory.search", {"query": "a" * 201}),
            ("knowledge.search", {"query": "a" * 301}), ("knowledge.search", {"query": "abc", "path": "/etc/passwd"}),
            ("knowledge.search", {"query": "abc", "generation": "two"}), ("game.command", {"command": "info"}),
        ]:
            with self.subTest(tool=tool, args=args), self.assertRaises(ValidationError):
                self.session.validate(tool, args)
        self.assertEqual(self.hub.started, [])

    def test_state_preserves_zero_false_null_and_marks_missing_unknown(self):
        result = self.execute("state.read", {"sections": ["vitals", "hands"]})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["vitals"]["health"]["value"], 0)
        self.assertFalse(result["data"]["dead"])
        self.assertIsNone(result["data"]["hands"]["right"])
        self.assertIn("roundtime", result["data"]["unknown_fields"])
        self.assertNotIn("room", result["data"])

    def test_fresh_character_data_is_reused_without_commands(self):
        result = self.execute("character.read")
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["data"]["categories"]["skills"]["freshness"]["current"])
        self.assertEqual(self.hub.started, [])

    def test_only_stale_requested_category_is_refreshed(self):
        self.hub.state["character_data"]["skills"].update(observed_at=None, source="infomon_cache", complete=False)
        result = self.execute("character.read")
        self.assertEqual(self.hub.started[0]["args"], {"categories": ["skills"]})
        self.assertEqual(self.hub.started[0]["expected_generation"], "one")
        self.assertLessEqual(self.hub.started[0]["timeout_seconds"], 3)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["data"]["categories"]["skills"]["freshness"]["current"])
        self.hub.capabilities.interrupt.assert_not_called()

    def test_cached_mode_never_refreshes_unknown_age(self):
        self.hub.state["character_data"]["skills"].update(observed_at=None, source="infomon_cache", complete=False)
        result = self.execute("character.read", {"categories": ["skills"], "freshness": "cached"})
        self.assertEqual(result["status"], "stale")
        self.assertEqual(self.hub.started, [])

    def test_level_change_and_old_timestamp_need_refresh(self):
        for change in ("level", "age"):
            with self.subTest(change=change):
                self.hub.state = snapshot()
                self.hub.started.clear()
                if change == "level":
                    self.hub.state["character_data"]["skills"]["observed_level"] = 89
                else:
                    self.hub.state["character_data"]["skills"]["observed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=121)).isoformat()
                self.execute("character.read", {"categories": ["skills"]})
                self.assertEqual(self.hub.started[0]["args"]["categories"], ["skills"])

    def test_denied_or_expired_recon_does_not_claim_freshness(self):
        self.hub.state["character_data"]["skills"]["observed_at"] = None
        for outcome in ("failed", "timed_out", "interrupted"):
            self.hub.outcome = outcome
            result = self.execute("character.read", {"categories": ["skills"]})
            self.assertEqual(result["status"], "unavailable")
            self.assertFalse(result["data"]["categories"]["skills"]["freshness"]["current"])

    def test_missing_live_session_still_allows_knowledge(self):
        self.hub.state = None
        session = EvidenceTools(self.hub).open("Testmage", self.control)
        self.addCleanup(session.close)
        self.assertEqual(session.execute("state.read", {}, self.control)["status"], "unavailable")
        self.assertEqual(session.execute("character.read", {}, self.control)["status"], "unavailable")
        self.assertEqual(session.execute("knowledge.search", {"query": "706"}, self.control)["status"], "success")
        self.assertEqual(self.hub.started, [])

    def test_absent_binding_never_adopts_new_session(self):
        self.hub.state = None
        session = EvidenceTools(self.hub).open("Testmage", self.control)
        self.addCleanup(session.close)
        self.hub.state = snapshot()
        with self.assertRaises(QuestionInvalidated):
            session.execute("character.read", {}, self.control)
        self.assertEqual(self.hub.started, [])

    def test_generation_change_before_or_during_recon_cancels_only_owned_work(self):
        self.hub.state["character_data"] = {}
        self.hub.on_watch = lambda: self.hub.state.update(generation="two")
        with self.assertRaises(QuestionInvalidated):
            self.execute("character.read")
        self.hub.capabilities.interrupt.assert_called_once_with("owned-op")

    def test_forget_during_wait_cancels_only_owned_work(self):
        self.hub.state["character_data"] = {}
        self.hub.on_watch = lambda: self.control.cancel("forget")
        with self.assertRaises(QuestionInvalidated):
            self.execute("character.read")
        self.hub.capabilities.interrupt.assert_called_once_with("owned-op")

    def test_cancel_during_admission_revokes_returned_owned_id(self):
        self.hub.state["character_data"] = {}
        original = self.hub.capabilities.start
        entered, release, cancelled = Event(), Event(), Event()
        errors = []
        def blocked(*args, **kwargs):
            operation = original(*args, **kwargs)
            entered.set()
            release.wait(2)
            return operation
        def execute():
            try:
                self.execute("character.read")
            except Exception as error:
                errors.append(error)
        def cancel():
            self.control.cancel("forget")
            cancelled.set()
        self.hub.capabilities.start = blocked
        worker = Thread(target=execute, daemon=True)
        canceller = Thread(target=cancel, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            canceller.start()
            self.assertTrue(self.control.cancelled.wait(1))
            self.assertFalse(cancelled.is_set(), "cancel must wait for the admitted ID")
        finally:
            release.set()
            worker.join(2)
            if canceller.ident is not None:
                canceller.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertTrue(cancelled.is_set())
        self.assertIsInstance(errors[0], QuestionInvalidated)
        self.hub.capabilities.interrupt.assert_called_once_with("owned-op")

    def test_cancel_revokes_broker_approval_synchronously_while_watch_is_blocked(self):
        self.hub.state["character_data"] = {}
        broker = ActionBroker()
        broker.admit_generation("Testmage", "one")
        broker.control(ActionControl("Testmage", True, "one"))
        unrelated = broker.submit(ActionProposal("Testmage", command="info", expected_generation="one", expected_room_id="228"))
        owned = {}
        waiting, release = Event(), Event()
        errors = []

        def start(*args, **kwargs):
            owned.update(broker.submit(ActionProposal("Testmage", commands=("info", "skills"), expected_generation="one", expected_room_id="228")))
            return SimpleNamespace(operation_id="owned-op", status="running", explanation="")

        def interrupt(operation_id):
            self.assertEqual(operation_id, "owned-op")
            broker.cancel(owned["action_id"], character="Testmage", generation="one")

        def watch(payload):
            waiting.set()
            release.wait(2)
            return {"operation": {"operation_id": "owned-op", "status": "interrupted"}, "cursor": "1"}

        def execute():
            try:
                self.execute("character.read")
            except Exception as error:
                errors.append(error)

        self.hub.capabilities = SimpleNamespace(start=start, interrupt=interrupt)
        self.hub.watch_operation = watch
        worker = Thread(target=execute, daemon=True)
        worker.start()
        try:
            self.assertTrue(waiting.wait(1))
            self.assertEqual(broker.get(owned["action_id"])["status"], "confirmation_required")
            self.control.cancel("forget")
            # The question worker still cannot leave watch: synchronous
            # callback revocation, not its polling finally, closed this gate.
            self.assertFalse(release.is_set())
            self.assertEqual(broker.get(owned["action_id"])["status"], "cancelled")
            self.assertEqual(broker.get(unrelated["action_id"])["status"], unrelated["status"])
            with self.assertRaises(ValidationError):
                broker.approve(ActionApproval(owned["action_id"], "Testmage", "228", generation="one"))
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], QuestionInvalidated)

    def test_expired_control_never_starts_recon(self):
        self.control.cancel("timeout")
        with self.assertRaises(QuestionTimeout):
            self.execute("character.read")
        self.assertEqual(self.hub.started, [])

    def test_stale_parent_snapshot_prevents_recon(self):
        self.hub.state["freshness"]["stale"] = True
        result = self.execute("character.read")
        self.assertNotEqual(result["status"], "success")
        self.assertEqual(self.hub.started, [])
        self.assertFalse(result["data"]["categories"]["info"]["freshness"]["current"])

    def test_historical_database_fallback_keeps_provenance(self):
        data = snapshot()["character_data"]["skills"]
        data["generation"] = "old"
        knowledge = SimpleNamespace(find=Mock(return_value={"skills": data}))
        self.hub.state["character_data"] = {}
        session = EvidenceTools(self.hub, knowledge).open("Testmage", self.control)
        self.addCleanup(session.close)
        result = session.execute("character.read", {"categories": ["skills"], "freshness": "cached"}, self.control)
        self.assertFalse(result["data"]["categories"]["skills"]["freshness"]["current"])
        self.assertEqual(result["sources"][0]["generation"], "old")
        self.assertEqual(result["sources"][0]["title"], "Testmage: training and spell-circle ranks")
        knowledge.find.assert_called_once_with(character="Testmage", game="GSIV", generation="one")

    def test_inventory_is_recorded_only_and_search_identity_bound(self):
        result = self.execute("inventory.search", {"query": "staff"})
        self.assertTrue(result["data"]["historical"])
        self.assertEqual(self.hub.queries, [{"character": "Testmage", "query": "staff"}])
        self.assertEqual(result["sources"][0]["source"], "recorded_inventory")
        self.assertEqual(self.hub.started, [])

    def test_search_retains_revision_and_only_included_sources(self):
        normal = self.hub.wiki_search({"query": "706"})
        normal["items"].append({"title": "Huge omitted page", "text": "z" * (MAX_EVIDENCE_CHARS + 1), "source": "other"})
        self.hub.wiki_search = lambda _: normal
        result = self.execute("knowledge.search", {"query": "706"})
        self.assertEqual(len(result["data"]["items"]), 1)
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["revision_id"], "234211")

    def test_default_provider_budget_retains_complete_long_mechanics_record(self):
        record = {"source": "local_gswiki", "title": "Synthetic mechanics", "revision_id": "2",
                  "text": "COMPLETE-MECHANICS " + "x" * 7000}
        self.hub.wiki_search = lambda _: {"items": [record], "total": 1}
        result = self.execute("knowledge.search", {"query": "synthetic mechanics"})
        self.assertEqual(len(result["data"]["items"]), 1)
        self.assertEqual(result["data"]["items"][0], record)
        self.assertEqual(result["sources"][0]["revision_id"], "2")

    def test_configured_provider_budget_changes_complete_record_selection(self):
        record = {"source": "local_gswiki", "title": "Synthetic mechanics", "revision_id": "2",
                  "text": "COMPLETE-MECHANICS " + "x" * 13000}
        self.hub.wiki_search = lambda _: {"items": [record], "total": 1}
        for limit, included in ((6000, False), (18000, True)):
            with self.subTest(limit=limit):
                session = EvidenceTools(self.hub, max_result_chars=limit).open("Testmage", self.control)
                self.addCleanup(session.close)
                result = session.execute("knowledge.search", {"query": "mechanics"}, self.control)
                self.assertEqual(result["data"]["items"], [record] if included else [])
                self.assertEqual(len(result["sources"]), int(included))
                self.assertLess(len(json.dumps(result, ensure_ascii=False)), limit)
                if not included:
                    self.assertEqual(result["status"], "partial")
                    self.assertEqual(result["data"]["total"], 1)
                    self.assertEqual(result["diagnostics"], [{"reason": "output_budget", "omitted_items": 1,
                                                              "configured_data_limit_chars": limit - 1500}])

    def test_omitted_inventory_is_partial_but_no_matches_remain_not_found(self):
        record = {"dossier_id": "oversized", "facts": [{"text": "x" * (MAX_EVIDENCE_CHARS + 1)}]}
        for items, expected in (([record], "partial"), ([], "not_found")):
            with self.subTest(expected=expected):
                self.hub.inventory_find = lambda _: {"items": items, "total": len(items)}
                result = self.execute("inventory.search", {"query": "record"})
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["data"]["total"], len(items))
                self.assertEqual(result["data"]["items"], [])
                self.assertEqual(result["sources"], [])
                if items:
                    self.assertEqual(result["diagnostics"][0]["omitted_items"], 1)

    def test_no_knowledge_matches_remain_not_found(self):
        self.hub.wiki_search = lambda _: {"items": [], "total": 0}
        self.assertEqual(self.execute("knowledge.search", {"query": "absent"})["status"], "not_found")

    def test_provider_budget_constructor_rejects_invalid_limits(self):
        for limit in (True, "12000", 2999, 100001):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                EvidenceTools(self.hub, max_result_chars=limit)

    def test_state_budget_omits_whole_oversize_field_with_diagnostic(self):
        self.hub.state["room"]["description"] = "x" * (MAX_EVIDENCE_CHARS + 1)
        result = self.execute("state.read", {"sections": ["room"]})
        self.assertNotIn("room", result["data"])
        self.assertEqual(result["diagnostics"][0]["omitted_fields"], ["room"])

    def test_close_without_work_does_not_stop_unrelated_operations(self):
        self.session.close()
        self.hub.capabilities.interrupt.assert_not_called()

    def test_source_failure_is_sanitized_and_still_checks_cancellation(self):
        self.hub.wiki_search = Mock(side_effect=OSError("private/path/token"))
        result = self.execute("knowledge.search", {"query": "706"})
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("private", str(result))

    def test_search_generation_change_discards_returned_private_evidence(self):
        original = self.hub.wiki_search
        def changed(payload):
            self.hub.state["generation"] = "two"
            return original(payload)
        self.hub.wiki_search = changed
        with self.assertRaises(QuestionInvalidated):
            self.execute("knowledge.search", {"query": "706"})

    def test_output_budget_omits_large_character_category_truthfully(self):
        self.hub.state["character_data"]["skills"]["values"]["skills"] = {
            str(i): {"ranks": i, "bonus": i * 2} for i in range(MAX_EVIDENCE_CHARS // 20)
        }
        result = self.execute("character.read", {"categories": ["skills"]})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["categories"], {})
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["diagnostics"][0]["omitted_category"], "skills")


if __name__ == "__main__":
    unittest.main()
