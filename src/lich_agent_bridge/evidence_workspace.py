"""Question-local evidence storage, separate from the bounded model context.

The request ceiling bounds retained memory; prompt selection is recomputed on
every turn. A manifest is a locator, never a substitute for omitted evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass
class _Entry:
    record: dict[str, Any]
    touched: int
    priority: int
    reactivated: bool = False


class EvidenceWorkspace:
    """Retain at most ``max_requests`` complete, individually bounded records.

    Reads outrank discovery. Within a class, recently requested evidence wins;
    repeating a cached request reactivates it without executing any operation.
    Sources describe only the current rendered selection, not cached contents.
    """

    def __init__(self, *, max_result_chars: int, max_context_chars: int, max_requests: int):
        self.max_result_chars = max_result_chars
        self.max_context_chars = max_context_chars
        self.max_requests = max_requests
        self._entries: list[_Entry] = []
        self._requests: dict[str, int] = {}
        self._identities: dict[str, int] = {}
        self._clock = 0
        self.diagnostics: list[dict[str, Any]] = []

    def activate(self, request: dict[str, Any]) -> bool:
        index = self._requests.get(_json(request))
        if index is None:
            return False
        self._clock += 1
        self._entries[index].touched = self._clock
        self._entries[index].reactivated = True
        return True

    def add(self, request: dict[str, Any], result: dict[str, Any]) -> None:
        key = _json(request)
        if self.activate(request):
            return
        if len(self._requests) >= self.max_requests:
            raise ValueError("question evidence request ceiling exceeded")
        identity = self._identity(request, result)
        if identity is not None and identity in self._identities:
            index = self._identities[identity]
            self._requests[key] = index
            self._clock += 1
            self._entries[index].touched = self._clock
            return
        record = {"evidence_id": f"evidence-{len(self._entries) + 1}",
                  "request": request, "result": result}
        size = len(_json(record))
        if size > self.max_result_chars:
            # Never retain an unbounded provider response or forward its sources.
            # The source session can still service a narrower read using handles.
            record = {"evidence_id": record["evidence_id"],
                      "request": {"tool": request["tool"]}, "result": {
                          "status": "omitted", "data": self._locator(result),
                          "detail": "Entire result omitted because it exceeded the evidence output budget; contents are unknown."}}
            self.diagnostics.append({
                "source": "evidence_loop", "status": "result_omitted", "tool": request["tool"],
                "detail": f"{request['tool']} result omitted: per_result evidence budget exceeded.",
                "limit": "per_result", "configured_limit_chars": self.max_result_chars,
                "result_chars": size, "remaining_chars": self.max_context_chars,
            })
            identity = None
        else:
            for diagnostic in result.get("diagnostics", ()):
                if diagnostic not in self.diagnostics:
                    self.diagnostics.append(diagnostic)
        self._clock += 1
        self._requests[key] = len(self._entries)
        if identity is not None:
            self._identities[identity] = len(self._entries)
        priority = {"knowledge.read": 3, "combat.report": 3, "state.read": 2, "character.read": 2,
                    "inventory.search": 1}.get(request["tool"], 0)
        self._entries.append(_Entry(record, self._clock, priority))

    def select(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Reserve actual serialized manifest space first. This makes even eight
        # evictions fit without relying on estimates about provider metadata.
        records = [self._manifest(entry.record) for entry in self._entries]
        selected: set[int] = set()
        discovery_seen: set[str] = set()
        order = sorted(range(len(self._entries)),
                       key=lambda i: (4 if self._entries[i].reactivated else self._entries[i].priority,
                                      self._entries[i].touched), reverse=True)
        for index in order:
            candidate = self._deduplicate_discovery(self._entries[index].record, discovery_seen)
            old = records[index]
            records[index] = candidate
            if len(_json(records)) <= self.max_context_chars:
                selected.add(index)
                discovery_seen.update(self._discovery_keys(candidate))
            else:
                records[index] = old
        sources: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            if index not in selected:
                continue
            for source in record["result"].get("sources", ()):
                if source not in sources:
                    sources.append(source)
        for entry in self._entries:
            entry.reactivated = False
        return records, sources

    @staticmethod
    def _locator(result: dict[str, Any]) -> dict[str, Any] | None:
        data = result.get("data")
        if not isinstance(data, dict):
            return None
        # Only bounded application-issued locators, never excerpt text or an
        # unbounded provider outline. Sources stay absent from manifests.
        fields = ("source_id", "section_id", "start", "end", "next_cursor")
        locator = {name: value for name in fields if (value := data.get(name)) is not None
                   and isinstance(value, (str, int)) and len(str(value)) <= 160}
        if not locator and isinstance(data.get("items"), list):
            # Keep all six application-issued discovery handles navigable even
            # when the original long query cannot fit the compact manifest.
            handles = []
            for item in data["items"]:
                if (isinstance(item, dict) and isinstance(item.get("source_id"), str)
                        and len(item["source_id"]) <= 160):
                    candidate = {"source_ids": handles + [item["source_id"]]}
                    if len(_json(candidate)) <= 200:
                        handles.append(item["source_id"])
                    if len(handles) == 6:
                        break
            locator = {"source_ids": handles} if handles else {}
        if len(_json(locator)) > 200:
            locator = {"source_id": locator["source_id"]} if "source_id" in locator else {}
        return locator or None

    def _manifest(self, record: dict[str, Any]) -> dict[str, Any]:
        if record["result"]["status"] == "omitted":
            return record
        request = record["request"]
        if len(_json(request)) > 180:
            request = {"tool": request["tool"]}
        locator = self._locator(record["result"])
        if locator and len(_json(locator)) > 200:
            locator = {"source_id": locator["source_id"]} if "source_id" in locator else None
        return {"evidence_id": record["evidence_id"],
                "request": request, "result": {
                    "status": "not_in_context", "data": locator,
                    "detail": "Retained, not supplied. Repeat the original request to reactivate, or read a listed source handle."}}

    @staticmethod
    def _identity(request: dict[str, Any], result: dict[str, Any]) -> str | None:
        if request["tool"] not in ("knowledge.search", "knowledge.read"):
            return None
        data = result.get("data")
        sources = result.get("sources", [])
        if isinstance(data, dict) and data.get("source_id") and "text" in data:
            provenance = data.get("provenance", {})
            # A deliberate read is identified by source snapshot and range,
            # never by the query, diagnostics, or entire response envelope.
            value = [request["tool"], data["source_id"], provenance.get("revision_id"),
                     data.get("section_id"), data.get("start"), data.get("end"),
                     result["status"], data["text"], data.get("complete"), data.get("next_cursor")]
        else:
            # Legacy providers without passage handles remain supported.
            value = [request["tool"], result["status"], data, sources]
        return hashlib.sha256(_json(value).encode()).hexdigest()

    @staticmethod
    def _item_key(item: dict[str, Any]) -> str:
        provenance = item.get("provenance", {})
        return _json([item.get("source_id"), provenance.get("revision_id"),
                      item.get("section_id"), item.get("start"), item.get("end"), item.get("snippet")])

    def _discovery_keys(self, record: dict[str, Any]) -> set[str]:
        if record["request"]["tool"] != "knowledge.search":
            return set()
        data = record["result"].get("data")
        items = data.get("items", ()) if isinstance(data, dict) else ()
        return {self._item_key(item) for item in items if isinstance(item, dict) and item.get("source_id")}

    def _deduplicate_discovery(self, record: dict[str, Any], seen: set[str]) -> dict[str, Any]:
        if not seen or record["request"]["tool"] != "knowledge.search":
            return record
        result = record["result"]
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return record
        items = [item for item in data["items"]
                 if not isinstance(item, dict) or not item.get("source_id") or self._item_key(item) not in seen]
        if len(items) == len(data["items"]):
            return record
        ids = {item.get("source_id") for item in items if isinstance(item, dict)}
        return {**record, "result": {**result, "data": {**data, "items": items,
                "discovery_note": "Duplicate discovery candidates already supplied in other selected results were removed."},
                "sources": [source for source in result.get("sources", ()) if source.get("source_id") in ids]}}
