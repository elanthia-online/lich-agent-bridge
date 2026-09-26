"""Read bounded recorder evidence retained on an existing operation, never SQL."""
from __future__ import annotations

import json
from typing import Mapping


def _reject_constant(value: str):
    raise ValueError(f"nonfinite JSON constant: {value}")


def operation_report(operation: Mapping) -> dict:
    base = {"operation_id": operation["operation_id"], "character": operation["character"],
            "operation_status": operation["status"], "ended_at": operation.get("ended_at"),
            "historical": True}
    if operation["status"] not in {"succeeded", "failed", "timed_out", "interrupted"}:
        return {**base, "status": "unavailable", "reason": "operation_not_terminal"}
    for evidence in reversed(operation.get("evidence", [])):
        if not isinstance(evidence, Mapping):
            continue
        facts = evidence.get("facts", {})
        if not isinstance(facts, Mapping):
            continue
        details = facts.get("details", {})
        if not isinstance(details, Mapping) or "combat_report_json" not in details:
            continue
        encoded = details["combat_report_json"]
        try:
            if not isinstance(encoded, str) or len(encoded) > 3500:
                raise ValueError("invalid report length")
            report = json.loads(encoded, parse_constant=_reject_constant)
            if (not isinstance(report, dict) or report.get("version") != 1
                    or report.get("status") not in {"observed", "partial", "unavailable"}
                    or not isinstance(report.get("trials"), list) or len(report["trials"]) > 5):
                raise ValueError("unsupported report")
        except (ValueError, TypeError, RecursionError):
            return {**base, "status": "unavailable", "reason": "invalid_combat_report"}
        if report["status"] != "unavailable" and details.get("recovery_complete") is not True:
            return {**base, "status": "unavailable", "reason": "safe_handoff_unverified"}
        return {**base, "status": report["status"], "report": report,
                "generation": evidence.get("generation"), "action_id": evidence.get("action_id"),
                "recovery_complete": details.get("recovery_complete") is True}
    return {**base, "status": "unavailable", "reason": "no_retained_combat_report"}
