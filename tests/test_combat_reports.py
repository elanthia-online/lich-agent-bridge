import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from lich_agent_bridge.combat_reports import operation_report
from lich_agent_bridge.errors import ValidationError
from lich_agent_bridge.session_hub import SessionHub
from lich_agent_bridge.evidence_tools import EvidenceTools
from lich_agent_bridge.question import QuestionControl
from .test_evidence_tools import Hub


def operation(**extra):
    report = {"version": 1, "status": "observed", "trials": [{"routine": "a", "own_attack_records": 2}]}
    return {"operation_id": "a" * 16, "character": "Testmage", "status": "succeeded", "ended_at": 100,
            "evidence": [{"generation": "old-generation", "action_id": "b" * 16,
                          "facts": {"details": {"combat_report_json": json.dumps(report), "recovery_complete": True}}}], **extra}


class CombatReportTests(unittest.TestCase):
    def test_exact_retained_historical_evidence(self):
        result = operation_report(operation())
        self.assertTrue(result["historical"])
        self.assertTrue(result["recovery_complete"])
        self.assertEqual(result["generation"], "old-generation")
        self.assertEqual(result["report"]["trials"][0]["own_attack_records"], 2)

    def test_in_progress_does_not_expose_terminal_evidence(self):
        self.assertEqual(operation_report(operation(status="running"))["reason"], "operation_not_terminal")

    def test_unavailable_report_retains_bounded_receipt_diagnostics(self):
        report = {"version": 1, "status": "unavailable", "trials": [],
                  "reason": "no_attributable_recorder_receipts",
                  "receipt_counts": {"received": 1, "accepted": 0, "missing_source": 1}}
        record = operation(evidence=[{"facts": {"details": {
            "combat_report_json": json.dumps(report), "recovery_complete": True}}}])
        self.assertEqual(operation_report(record)["report"], report)

    def test_observed_report_cannot_override_failed_safe_handoff(self):
        record = operation()
        record["evidence"][0]["facts"]["details"]["recovery_complete"] = False
        self.assertEqual(operation_report(record)["reason"], "safe_handoff_unverified")

    def test_missing_or_malformed_evidence_is_unavailable(self):
        self.assertEqual(operation_report(operation(evidence=[]))["reason"], "no_retained_combat_report")
        for encoded in [None, "x" * 3501, '{"version":NaN}', '[]', '{"version":2}', '{']:
            record = operation(evidence=[{"facts": {"details": {"combat_report_json": encoded}}}])
            self.assertEqual(operation_report(record)["reason"], "invalid_combat_report")
        self.assertEqual(operation_report(operation(evidence=[None, {"facts": None}]))["status"], "unavailable")

    def hub(self, character="Testmage", capability="controller.hunt"):
        hub = SessionHub.__new__(SessionHub)
        record = SimpleNamespace(character=character, capability=capability)
        hub.capabilities = SimpleNamespace(get=Mock(return_value=record), history=Mock(return_value=[record]))
        hub._operation_mapping = Mock(return_value=operation(character=character))
        return hub

    def test_lookup_accepts_only_bound_character_and_operation_id(self):
        hub = self.hub()
        self.assertEqual(hub.combat_report({"character": "Testmage", "operation_id": "a" * 16})["status"], "observed")
        for payload in [{"character": "Other", "operation_id": "a" * 16},
                        {"character": "Testmage", "operation_id": "a"},
                        {"character": "Testmage", "sql": "SELECT *"},
                        {"character": "Testmage", "path": "/tmp/anything"}]:
            with self.assertRaises(ValidationError):
                hub.combat_report(payload)

        hub = self.hub(capability="inventory.inspect")
        with self.assertRaises(ValidationError):
            hub.combat_report({"character": "Testmage", "operation_id": "a" * 16})

    def test_latest_is_character_scoped_and_never_uses_non_controller_history(self):
        hub = self.hub()
        self.assertEqual(hub.combat_report({"character": "Testmage"})["status"], "observed")
        hub.capabilities.history.assert_called_once_with("Testmage")
        hub = self.hub(capability="inventory.inspect")
        self.assertEqual(hub.combat_report({"character": "Testmage"})["status"], "unavailable")
        hub._operation_mapping.assert_not_called()


class CombatEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.control = QuestionControl(3)
        self.session = EvidenceTools(self.hub).open("Testmage", self.control, read_only=True)
        self.addCleanup(self.session.close)

    def execute(self, tool, args):
        return self.session.execute(tool, args, self.control)

    def test_combat_report_reads_without_starting_an_action(self):
        self.hub.combat_report = Mock(return_value=operation_report(operation()))
        result = self.execute("combat.report", {"operation_id": "a" * 16})
        self.hub.combat_report.assert_called_once_with({"character": "Testmage", "operation_id": "a" * 16})
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["sources"][0]["authority"], "historical_recorder_observation")
        self.assertEqual(result["sources"][0]["title"], "Testmage: controller trial report")
        self.assertEqual(self.hub.started, [])

    def test_unavailable_report_has_diagnostic_without_inventing_a_reference(self):
        self.hub.combat_report = Mock(return_value={"status": "unavailable", "reason": "no_retained_controller_operation"})
        result = self.execute("combat.report", {})
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["diagnostics"], [{"source": "combat.report", "status": "unavailable",
                                                "detail": "no_retained_controller_operation"}])

    def test_combat_report_disallows_character_path_and_sql_overrides(self):
        for args in [{"character": "Other"}, {"database": "x"}, {"sql": "x"}, {"operation_id": "wrong"}]:
            with self.assertRaises(ValidationError):
                self.execute("combat.report", args)
