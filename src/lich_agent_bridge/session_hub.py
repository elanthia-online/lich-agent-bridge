"""Shared facade for local CLI/MCP reads and bounded capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
import math
import re
import time
from typing import Any, Mapping, Sequence

from .actions import ActionBroker
from .controller_manifest import ControllerManifest
from .errors import ValidationError
from .inventory import InventoryItem, InventoryKnowledge
from .knowledge import KnowledgeBase, KnowledgeExcerpt
from .operations import (
    CapabilityRunner,
    EvidenceRecord,
    HandItem,
    ItemBinding,
    ItemState,
    NearbyObject,
    OperationRecord,
    OwnedLocation,
    SessionState,
)
from .world_state import MAX_WATCH_TIMEOUT_SECONDS, WorldState
from .timings import emit_timing
from .watchers import WatcherReducer

MAX_CHARACTER_LENGTH = 40
MAX_WIKI_QUERY_LENGTH = 512
MAX_WIKI_RESULTS = 20
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def _strict(
    value: Any, *, label: str, allowed: set[str], required: set[str]
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be an object")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise ValidationError(
            f"unsupported {label} field(s): {', '.join(sorted(unknown))}"
        )
    if missing:
        raise ValidationError(
            f"missing {label} field(s): {', '.join(sorted(missing))}"
        )
    return value


def _text(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    result = value.replace("\x00", "").strip()
    if not result:
        raise ValidationError(f"{field} must not be blank")
    if len(result) > maximum:
        raise ValidationError(f"{field} exceeds {maximum} characters")
    return result


def _character(value: Any) -> str:
    return _text(value, "character", maximum=MAX_CHARACTER_LENGTH)


@dataclass(frozen=True, slots=True)
class EvidenceRegistration:
    operation_id: str
    character: str
    generation: str
    method: str
    object_id: str
    cursor: int


@dataclass(frozen=True, slots=True)
class ControllerEvidenceRegistration:
    operation_id: str
    controller: str
    character: str
    generation: str
    cursor: int


class WorldStateEvidenceAdapter:
    """Accept only explicitly attributed events after expectation registration."""

    def __init__(self, world_state: WorldState):
        self._world_state = world_state

    def register(
        self, operation_id: str, method: str, binding: ItemBinding
    ) -> EvidenceRegistration:
        current = self._world_state.snapshot(binding.character)
        if current is None:
            raise ValidationError("live snapshot is unavailable")
        snapshot = current["snapshot"]
        if snapshot["generation"] != binding.generation:
            raise ValidationError("stale session generation")
        return EvidenceRegistration(
            operation_id=operation_id,
            character=binding.character,
            generation=binding.generation,
            method=method,
            object_id=binding.object_id,
            cursor=int(current["cursor"]),
        )

    def verify(
        self, registration: object, action_id: str | None = None
    ) -> EvidenceRecord | None:
        if not isinstance(registration, EvidenceRegistration):
            raise ValidationError("evidence registration is invalid")
        page = self._world_state.watch(
            registration.character,
            cursor=registration.cursor,
            timeout=0,
        )
        for event in page["events"]:
            data = event.get("data")
            if not isinstance(data, Mapping):
                continue
            if (
                event.get("kind") != "item_diagnostic"
                or event.get("generation") != registration.generation
                or str(data.get("method", "")).casefold()
                != registration.method.casefold()
                or str(data.get("object_id", "")).removeprefix("#")
                != registration.object_id
                or data.get("success") is not True
                or not action_id
                or str(data.get("action_id", "")) != action_id
            ):
                continue
            raw_facts = data.get("facts", {})
            facts = dict(raw_facts) if isinstance(raw_facts, Mapping) else {}
            detail = data.get("detail")
            if not isinstance(detail, str) or not detail.strip():
                detail = str(event.get("summary", "diagnostic evidence observed"))
            return EvidenceRecord(
                method=registration.method,
                generation=registration.generation,
                object_id=registration.object_id,
                detail=detail.strip(),
                facts=facts,
                action_id=action_id,
            )
        return None

    def register_controller(
        self,
        operation_id: str,
        controller: str,
        character: str,
        generation: str,
    ) -> ControllerEvidenceRegistration:
        current = self._world_state.snapshot(character)
        if current is None:
            raise ValidationError("live snapshot is unavailable")
        if current["snapshot"]["generation"] != generation:
            raise ValidationError("stale session generation")
        return ControllerEvidenceRegistration(
            operation_id=operation_id,
            controller=controller,
            character=character,
            generation=generation,
            cursor=int(current["cursor"]),
        )

    def verify_controller_recovery(self, *, character, controller, previous_generation,
                                   generation, action_id, room_id, hands) -> bool:
        """Read a player acknowledgement from the authenticated native event feed.

        No receipt means no cross-generation recovery; expired ring entries must
        be explicitly republished by the player, not inferred from current safety.
        """
        page = self._world_state.watch(character, cursor=0, timeout=0)
        for event in page["events"]:
            data = event.get("data")
            if (event.get("kind") != "controller_recovery" or event.get("generation") != generation
                    or not isinstance(data, Mapping)):
                continue
            if (data.get("controller") == controller and data.get("action_id") == action_id
                    and data.get("previous_generation") == previous_generation
                    and data.get("operator_confirmed") is True and data.get("room_id") == room_id
                    and data.get("hands") == {"left": hands[0], "right": hands[1]}):
                return True
        return False

    def verify_controller(
        self,
        registration: object,
        action_id: str,
        timeout_seconds: float,
    ) -> EvidenceRecord | None:
        if not isinstance(registration, ControllerEvidenceRegistration):
            raise ValidationError("controller evidence registration is invalid")
        if not action_id:
            raise ValidationError("controller action ID is unavailable")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout):
            raise ValidationError("controller evidence timeout must be finite")
        timeout = max(0.0, timeout)
        # The caller supplies the operation's remaining lifetime. A watch is
        # only one bounded transport wait, not a second operation deadline.
        deadline = time.monotonic() + timeout
        cursor = registration.cursor
        while True:
            remaining = deadline - time.monotonic()
            page = self._world_state.watch(
                registration.character,
                cursor=cursor,
                timeout=max(0.0, min(remaining, MAX_WATCH_TIMEOUT_SECONDS)),
            )
            cursor = int(page["cursor"])
            for event in page["events"]:
                data = event.get("data")
                if (
                    event.get("kind") != "controller_result"
                    or event.get("generation") != registration.generation
                    or not isinstance(data, Mapping)
                    or str(data.get("controller", "")).casefold()
                    != registration.controller.casefold()
                    or str(data.get("action_id", "")) != action_id
                ):
                    continue
                ok = data.get("ok")
                code = data.get("code")
                message = data.get("message")
                details = data.get("details")
                if (
                    not isinstance(ok, bool)
                    or not isinstance(code, str)
                    or not code.strip()
                    or not isinstance(message, str)
                    or not message.strip()
                    or not isinstance(details, Mapping)
                ):
                    raise ValidationError("controller result event is malformed")
                return EvidenceRecord(
                    method=f"controller.{registration.controller}",
                    generation=registration.generation,
                    object_id=registration.controller,
                    detail=message.strip(),
                    facts={
                        "ok": ok,
                        "code": code.strip(),
                        "message": message.strip(),
                        "details": dict(details),
                    },
                    action_id=action_id,
                )
            if time.monotonic() >= deadline:
                return None


class WorldStateOperationAdapter:
    """Bind only current hand IDs to unique durable inventory dossiers.

    Live hand location comes exclusively from ``WorldState``. Historical
    inventory locations are never consulted for ownership or current location.
    """

    def __init__(self, world_state: WorldState, inventory: InventoryKnowledge):
        self._world_state = world_state
        self._inventory = inventory

    def snapshot(self, character: str) -> SessionState:
        current = self._world_state.snapshot(character)
        if current is None:
            raise ValidationError("live snapshot is unavailable")
        if current["freshness"]["stale"]:
            raise ValidationError("live snapshot is stale")
        snapshot = current["snapshot"]
        room = snapshot.get("room")
        room_id = room.get("id") if isinstance(room, Mapping) else None
        if not isinstance(room_id, str) or not room_id:
            raise ValidationError("live room ID is unavailable")

        items: list[ItemState] = []
        live_hands: dict[str, HandItem | None] = {}
        hands = snapshot.get("hands")
        if isinstance(hands, Mapping):
            for side in ("right", "left"):
                held = hands.get(side)
                if not isinstance(held, Mapping):
                    live_hands[side] = None
                    continue
                object_id = str(held.get("id", "")).removeprefix("#")
                if not object_id:
                    live_hands[side] = None
                    continue
                live_hands[side] = HandItem(
                    object_id=object_id,
                    name=str(held.get("name", "")),
                )
                dossiers = self._matching_dossiers(character, object_id)
                if not dossiers:
                    dossiers = (
                        self._live_hand_identity(
                            character=str(snapshot["character"]),
                            generation=str(snapshot["generation"]),
                            object_id=object_id,
                            side=side,
                            name=str(held.get("name", "")),
                        ),
                    )
                for dossier in dossiers:
                    items.append(
                        ItemState(
                            object_id=object_id,
                            dossier_id=dossier.dossier_id,
                            fingerprint=dossier.fingerprint,
                            location=OwnedLocation(
                                location_id=f"{side}_hand",
                                kind=f"{side}_hand",
                                owner=str(snapshot["character"]),
                                verified_owned=True,
                                safety_rank=100,
                            ),
                        )
                    )
        nearby = snapshot.get("nearby")

        def nearby_objects(group: str) -> tuple[NearbyObject, ...] | None:
            if not isinstance(nearby, Mapping) or group not in nearby:
                return None
            raw = nearby.get(group)
            if not isinstance(raw, list):
                return None
            return tuple(
                NearbyObject(
                    object_id=str(item.get("id", "")).removeprefix("#"),
                    noun=str(item.get("noun", "")),
                    name=(str(item["name"]) if item.get("name") is not None else None),
                )
                for item in raw
                if isinstance(item, Mapping)
            )

        encumbrance = snapshot.get("encumbrance")
        if isinstance(encumbrance, str) and encumbrance.strip().lstrip("-").isdigit():
            encumbrance = int(encumbrance)
        scripts = snapshot.get("scripts")
        owners = snapshot.get("owners")
        script_status = snapshot.get("script_status")
        return SessionState(
            character=str(snapshot["character"]),
            generation=str(snapshot["generation"]),
            room_id=room_id,
            items=tuple(items),
            fresh=not bool(current["freshness"]["stale"]),
            sequence=int(snapshot["sequence"]),
            dead=snapshot.get("dead"),
            stunned=snapshot.get("stunned"),
            wounds=(
                dict(snapshot["wounds"])
                if isinstance(snapshot.get("wounds"), Mapping)
                else None
            ),
            encumbrance=(
                encumbrance
                if isinstance(encumbrance, int) and not isinstance(encumbrance, bool)
                else None
            ),
            hands=live_hands if isinstance(hands, Mapping) else None,
            scripts=(
                tuple(str(name) for name in scripts)
                if isinstance(scripts, list)
                else None
            ),
            owners=dict(owners) if isinstance(owners, Mapping) else None,
            nearby_creatures=nearby_objects("creatures"),
            nearby_corpses=nearby_objects("corpses"),
            script_status=(
                {str(name): str(status) for name, status in script_status.items()}
                if isinstance(script_status, Mapping)
                else None
            ),
            character_data=(
                deepcopy(dict(snapshot["character_data"]))
                if isinstance(snapshot.get("character_data"), Mapping) else None
            ),
        )

    def safest_owned_locations(
        self, character: str, object_id: str
    ) -> Sequence[OwnedLocation]:
        state = self.snapshot(character)
        return tuple(
            item.location for item in state.items if item.object_id == object_id
        )

    def _matching_dossiers(
        self, character: str, object_id: str
    ) -> tuple[InventoryItem, ...]:
        candidates = self._inventory.find(
            character=character,
            query=f"#{object_id}",
            limit=100,
        )
        return tuple(
            item
            for item in candidates
            if item.last_game_id == object_id
            and bool(item.dossier_id)
            and bool(item.fingerprint)
        )

    @staticmethod
    def _live_hand_identity(
        *,
        character: str,
        generation: str,
        object_id: str,
        side: str,
        name: str,
    ) -> InventoryItem:
        """Create an exact session-only identity when no durable dossier exists.

        This never reattaches durable knowledge. The active generation and
        exact live object ID remain the identity authority; side/name changes
        alter the fingerprint and therefore fail an in-flight binding check.
        """

        normalized_name = " ".join(name.casefold().split())
        material = "\x00".join(
            (
                "lich-live-hand-v1",
                character.casefold(),
                generation,
                object_id,
                side,
                normalized_name,
            )
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return InventoryItem(
            dossier_id=f"live-hand:{digest[:24]}",
            fingerprint=f"lich-live-hand-v1:{digest}",
            item_type="",
            noun="",
            name=name,
            full_name=name,
            last_game_id=object_id,
            last_seen_at=None,
            last_location=None,
            facts=(),
        )


class SessionHub:
    """One production interface shared by HTTP, CLI, and the MCP adapter."""

    def __init__(
        self,
        *,
        world_state: WorldState,
        actions: ActionBroker,
        inventory: InventoryKnowledge,
        knowledge: KnowledgeBase,
        capabilities: CapabilityRunner | None = None,
        controller_manifest: ControllerManifest | None = None,
        watchers: WatcherReducer | None = None,
        timing=None,
        monotonic=time.monotonic,
    ):
        self.world_state = world_state
        self.actions = actions
        self.inventory = inventory
        self.knowledge = knowledge
        self.watchers = watchers
        self._timing = timing
        self._monotonic = monotonic
        self.state_adapter = WorldStateOperationAdapter(world_state, inventory)
        self.evidence_adapter = WorldStateEvidenceAdapter(world_state)
        self.capabilities = capabilities or CapabilityRunner(
            actions=actions,
            state=self.state_adapter,
            evidence=self.evidence_adapter,
            timing=timing,
            controller_manifest=controller_manifest,
        )

    def snapshot(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        started = self._monotonic()
        request = _strict(
            payload,
            label="snapshot request",
            allowed={"character"},
            required={"character"},
        )
        character = _character(request["character"])
        current = self.world_state.snapshot(character)
        if current is None:
            raise ValidationError("live snapshot is unavailable")
        result = {
            **current["snapshot"],
            "cursor": str(current["cursor"]),
            "freshness": current["freshness"],
        }
        emit_timing(
            self._timing,
            "snapshot.read_ms",
            (self._monotonic() - started) * 1_000,
            character=character,
        )
        age = current["freshness"].get("age_seconds")
        if isinstance(age, (int, float)) and not isinstance(age, bool):
            emit_timing(
                self._timing,
                "snapshot.age_ms",
                float(age) * 1_000,
                character=character,
            )
        return result

    def watch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _strict(
            payload,
            label="watch request",
            allowed={"character", "cursor", "timeout_ms"},
            required={"character"},
        )
        character = _character(request["character"])
        raw_cursor = request.get("cursor", "0")
        if isinstance(raw_cursor, bool) or not isinstance(raw_cursor, (str, int)):
            raise ValidationError("cursor must be a nonnegative integer string")
        try:
            cursor = int(raw_cursor)
        except ValueError as error:
            raise ValidationError("cursor must be a nonnegative integer string") from error
        raw_timeout = request.get("timeout_ms", 0)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int):
            raise ValidationError("timeout_ms must be an integer")
        if not 0 <= raw_timeout <= int(MAX_WATCH_TIMEOUT_SECONDS * 1_000):
            raise ValidationError("timeout_ms must be between 0 and 30000")
        page = self.world_state.watch(
            character,
            cursor=cursor,
            timeout=raw_timeout / 1_000,
        )
        return {
            "items": page["events"],
            "total": len(page["events"]),
            "cursor": str(page["cursor"]),
            "timed_out": page["timed_out"],
            "truncated": page["truncated"],
        }

    def combat_report(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        from .combat_reports import operation_report

        request = _strict(payload, label="combat report request",
                          allowed={"character", "operation_id"}, required={"character"})
        character = _character(request["character"])
        if "operation_id" in request:
            operation_id = request["operation_id"]
            if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{16}", operation_id):
                raise ValidationError("operation_id must be a LAB operation ID")
            operation = self.capabilities.get(operation_id)
            if (operation.character.casefold() != character.casefold()
                    or not operation.capability.startswith("controller.")):
                raise ValidationError("operation was not found for this character")
        else:
            history = [op for op in self.capabilities.history(character)
                       if op.capability.startswith("controller.")]
            if not history:
                return {"character": character, "status": "unavailable", "reason": "no_retained_controller_operation"}
            operation = history[-1]
        return operation_report(self._operation_mapping(operation))

    def inventory_find(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _strict(
            payload,
            label="inventory find request",
            allowed={"character", "query"},
            required={"character", "query"},
        )
        character = _character(request["character"])
        query = _text(request["query"], "query", maximum=200)
        items = self.inventory.find(character=character, query=query)
        mappings = [item.to_mapping() for item in items]
        return {"items": mappings, "total": len(mappings)}

    def wiki_search(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _strict(
            payload,
            label="wiki search request",
            allowed={"query", "character", "limit"},
            required={"query"},
        )
        query = _text(
            request["query"], "query", maximum=MAX_WIKI_QUERY_LENGTH
        )
        character = (
            ""
            if request.get("character") is None
            else _character(request["character"])
        )
        limit = request.get("limit", MAX_WIKI_RESULTS)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError("limit must be an integer")
        if not 1 <= limit <= MAX_WIKI_RESULTS:
            raise ValidationError(f"limit must be between 1 and {MAX_WIKI_RESULTS}")
        result = self.knowledge.search(character=character, question=query)
        excerpts = getattr(result, "excerpts", tuple(result))
        diagnostics = getattr(result, "diagnostics", ())
        items = [self._knowledge_mapping(item) for item in excerpts[:limit]]
        return {
            "items": items,
            "total": len(excerpts),
            "diagnostics": [
                item.to_mapping() if hasattr(item, "to_mapping") else dict(item)
                for item in diagnostics
            ],
        }

    def alerts(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _strict(
            payload,
            label="alerts request",
            allowed={"character"},
            required={"character"},
        )
        character = _character(request["character"])
        items = (
            []
            if self.watchers is None
            else [alert.to_mapping() for alert in self.watchers.active(character)]
        )
        return {"items": items, "total": len(items)}

    def capability_catalog(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _strict(
            payload,
            label="capabilities request",
            allowed={"character"},
            required=set(),
        )
        character = (
            None
            if request.get("character") is None
            else _character(request["character"])
        )
        items = list(self.capabilities.describe_capabilities(character))
        return {"character": character, "items": items, "total": len(items)}

    def perform(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        character, capability, arguments, generation, timeout = self._perform_request(payload)
        options = {} if generation is None else {"expected_generation": generation}
        if timeout is not None:
            options["timeout_seconds"] = timeout
        operation = self.capabilities.perform(character, capability, arguments, **options)
        return self._operation_mapping(operation)

    def start_operation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Admit an operation and return immediately with its stable ID."""

        character, capability, arguments, generation, timeout = self._perform_request(payload)
        options = {} if generation is None else {"expected_generation": generation}
        if timeout is not None:
            options["timeout_seconds"] = timeout
        operation = self.capabilities.start(character, capability, arguments, **options)
        return self._operation_mapping(operation)

    def watch_operation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _strict(
            payload,
            label="operation watch request",
            allowed={"operation_id", "cursor", "timeout_ms"},
            required={"operation_id"},
        )
        operation_id = _text(request["operation_id"], "operation_id", maximum=64)
        raw_cursor = request.get("cursor", 0)
        if isinstance(raw_cursor, bool) or not isinstance(raw_cursor, (str, int)):
            raise ValidationError("cursor must be a nonnegative integer string")
        try:
            cursor = int(raw_cursor)
        except ValueError as error:
            raise ValidationError("cursor must be a nonnegative integer string") from error
        if cursor < 0:
            raise ValidationError("cursor must be nonnegative")
        timeout_ms = request.get("timeout_ms", 0)
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise ValidationError("timeout_ms must be an integer")
        if not 0 <= timeout_ms <= int(MAX_WATCH_TIMEOUT_SECONDS * 1_000):
            raise ValidationError("timeout_ms must be between 0 and 30000")
        events, operation = self.capabilities.wait(
            operation_id,
            after_cursor=cursor,
            timeout_seconds=timeout_ms / 1_000,
        )
        items = [self._operation_event_mapping(item) for item in events]
        next_cursor = cursor if not items else int(items[-1]["cursor"])
        return {
            "operation": self._operation_mapping(operation),
            "items": items,
            "total": len(items),
            "cursor": str(next_cursor),
            "timed_out": not items,
        }

    def stop_operation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _strict(
            payload,
            label="operation stop request",
            allowed={"character", "operation_id", "expected_generation"},
            required={"character"},
        )
        character = _character(request["character"])
        if "operation_id" in request or "expected_generation" in request:
            if not {"operation_id", "expected_generation"}.issubset(request):
                raise ValidationError("exact stop requires operation_id and expected_generation")
            operation_id = _text(request["operation_id"], "operation_id", maximum=64)
            generation = _text(request["expected_generation"], "expected_generation", maximum=128)
            operation = self.capabilities.get(operation_id)
            bound_generation = (operation.start_state.generation if operation.start_state is not None
                                else operation.expected_generation)
            if operation.character.casefold() != character.casefold() or generation != bound_generation:
                raise ValidationError("stop identity does not match the operation")
            if operation.status in {"succeeded", "failed", "timed_out", "interrupted"}:
                return {"character": character, "stopped": False, "operation_id": operation_id,
                        "reason": "operation_already_terminal"}
            self.capabilities.interrupt(operation_id)
            return {"character": character, "stopped": True, "stop_requested": True,
                    "operation_id": operation_id}
        active = [
            operation
            for operation in self.capabilities.history(character)
            if operation.status not in {"succeeded", "failed", "timed_out", "interrupted"}
        ]
        if not active:
            return {"character": character, "stopped": False, "reason": "no_active_operation"}
        if len(active) != 1:
            raise ValidationError("multiple active operations require explicit inspection")
        if self.capabilities.is_test_operation(active[0]):
            raise ValidationError("test run stop requires operation_id and expected_generation")
        self.capabilities.interrupt(active[0].operation_id)
        return {
            "character": character,
            "stopped": True,
            "operation_id": active[0].operation_id,
        }

    def control_operation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Admit a registered control on one exact operation, not another launch."""
        fields = {"character", "operation_id", "expected_generation", "control"}
        request = _strict(payload, label="operation control request", allowed=fields, required=fields)
        return self.capabilities.control_controller(
            _text(request["operation_id"], "operation_id", maximum=64),
            character=_character(request["character"]),
            expected_generation=_text(request["expected_generation"], "expected_generation", maximum=128),
            control=_text(request["control"], "control", maximum=16),
        )

    @staticmethod
    def _perform_request(
        payload: Mapping[str, Any],
    ) -> tuple[str, str, dict[str, Any], str | None, float | None]:
        request = _strict(
            payload,
            label="perform request",
            allowed={"character", "capability", "args", "expected_generation", "timeout_seconds"},
            required={"character", "capability"},
        )
        character = _character(request["character"])
        capability = _text(request["capability"], "capability", maximum=64)
        if _CAPABILITY.fullmatch(capability) is None:
            raise ValidationError("capability has invalid syntax")
        arguments = request.get("args", {})
        if not isinstance(arguments, Mapping):
            raise ValidationError("args must be an object")
        generation = (None if "expected_generation" not in request else
                      _text(request["expected_generation"], "expected_generation", maximum=128))
        timeout = request.get("timeout_seconds")
        if "timeout_seconds" in request:
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or not 0 < timeout <= 300):
                raise ValidationError("timeout_seconds must be positive and at most 300")
            timeout = float(timeout)
        return character, capability, dict(arguments), generation, timeout

    @staticmethod
    def _knowledge_mapping(excerpt: KnowledgeExcerpt) -> dict[str, Any]:
        return {
            "authority": excerpt.authority,
            "title": excerpt.title,
            "text": excerpt.text,
            "source": excerpt.source,
            "url": excerpt.url,
            "revision_id": excerpt.revision_id,
            "retrieved_at": excerpt.retrieved_at,
        }

    @staticmethod
    def _operation_mapping(operation: OperationRecord) -> dict[str, Any]:
        return {
            "operation_id": operation.operation_id,
            "capability": operation.capability,
            "character": operation.character,
            "args": operation.arguments,
            "status": operation.status,
            "requested_at": operation.requested_at,
            "admitted_at": operation.admitted_at,
            "started_at": operation.started_at,
            "ended_at": operation.ended_at,
            "binding": SessionHub._binding_mapping(operation.binding),
            "start_state": SessionHub._state_mapping(operation.start_state),
            "end_state": SessionHub._state_mapping(operation.end_state),
            "evidence": [
                {
                    "method": item.method,
                    "generation": item.generation,
                    "object_id": item.object_id,
                    "action_id": item.action_id,
                    "detail": item.detail,
                    "facts": dict(item.facts),
                }
                for item in operation.evidence
            ],
            "progress": [
                SessionHub._operation_event_mapping(item)
                for item in operation.events
            ],
            "alerts": list(operation.alerts),
            "explanation": operation.explanation,
        }

    @staticmethod
    def _operation_event_mapping(item: Any) -> dict[str, Any]:
        return {
            "cursor": item.cursor,
            "status": item.status,
            "detail": item.detail,
            "timestamp": item.timestamp,
        }

    @staticmethod
    def _binding_mapping(binding: ItemBinding | None) -> dict[str, Any] | None:
        if binding is None:
            return None
        return {
            "character": binding.character,
            "generation": binding.generation,
            "object_id": binding.object_id,
            "dossier_id": binding.dossier_id,
            "fingerprint": binding.fingerprint,
            "original_location": SessionHub._location_mapping(
                binding.original_location
            ),
        }

    @staticmethod
    def _state_mapping(state: SessionState | None) -> dict[str, Any] | None:
        if state is None:
            return None
        return {
            "character": state.character,
            "generation": state.generation,
            "room_id": state.room_id,
            **({"character_data": deepcopy(dict(state.character_data)), "sequence": state.sequence}
               if state.character_data is not None else {}),
            "items": [
                {
                    "object_id": item.object_id,
                    "dossier_id": item.dossier_id,
                    "fingerprint": item.fingerprint,
                    "location": SessionHub._location_mapping(item.location),
                }
                for item in state.items
            ],
        }

    @staticmethod
    def _location_mapping(location: OwnedLocation) -> dict[str, Any]:
        return {
            "location_id": location.location_id,
            "kind": location.kind,
            "owner": location.owner,
            "verified_owned": location.verified_owned,
            "safety_rank": location.safety_rank,
        }
