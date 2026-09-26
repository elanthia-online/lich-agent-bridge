"""Strict messages exchanged across the authenticated Lich/sidecar seam."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Mapping

from .errors import ValidationError

MAX_CHARACTER_LENGTH = 40
MAX_EVENT_TEXT_LENGTH = 4_000
MAX_QUESTION_LENGTH = 2_000
MAX_GENERATION_LENGTH = 128
MAX_SESSION_ID_LENGTH = 128
MAX_STATE_TEXT_LENGTH = 512
MAX_STATE_ITEMS = 256
MAX_JSON_DEPTH = 5
MAX_JSON_KEYS = 64
MAX_SEQUENCE = 2**63 - 1


def _strict_keys(
    value: Mapping[str, Any], *, allowed: set[str], required: set[str]
) -> None:
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise ValidationError(f"unsupported field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ValidationError(f"missing field(s): {', '.join(sorted(missing))}")


def _text(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    normalized = value.replace("\x00", "").strip()
    if not normalized:
        raise ValidationError(f"{field} must not be blank")
    if len(normalized) > maximum:
        raise ValidationError(f"{field} exceeds {maximum} characters")
    return normalized


def _optional_text(value: Any, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum=maximum)


def _integer(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SEQUENCE,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValidationError(f"{field} must be between {minimum} and {maximum}")
    return value


def _number(value: Any, field: str, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= maximum:
        raise ValidationError(f"{field} must be between 0 and {maximum:g}")
    return result


def _timestamp(value: Any, field: str = "observed_at") -> str:
    result = _text(value, field, maximum=64)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} must include a timezone")
    return result


def _bounded_json(value: Any, field: str, *, depth: int = 0) -> Any:
    """Copy and bound JSON-shaped event/state details."""

    if depth > MAX_JSON_DEPTH:
        raise ValidationError(f"{field} is nested too deeply")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_SEQUENCE:
            raise ValidationError(f"{field} integer is out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"{field} number must be finite")
        return value
    if isinstance(value, str):
        if len(value) > MAX_EVENT_TEXT_LENGTH:
            raise ValidationError(
                f"{field} string exceeds {MAX_EVENT_TEXT_LENGTH} characters"
            )
        return value.replace("\x00", "")
    if isinstance(value, list):
        if len(value) > MAX_STATE_ITEMS:
            raise ValidationError(f"{field} has too many items")
        return [
            _bounded_json(item, f"{field}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        if len(value) > MAX_JSON_KEYS:
            raise ValidationError(f"{field} has too many fields")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValidationError(f"{field} keys must be nonblank bounded strings")
            result[key] = _bounded_json(item, f"{field}.{key}", depth=depth + 1)
        return result
    raise ValidationError(f"{field} must contain only JSON values")


def _room(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("room must be an object or null")
    _strict_keys(value, allowed={"id", "title"}, required=set())
    result: dict[str, str] = {}
    if value.get("id") is not None:
        room_id = value["id"]
        if not isinstance(room_id, (str, int)) or isinstance(room_id, bool):
            raise ValidationError("room.id must be a string, integer, or null")
        result["id"] = _text(str(room_id), "room.id", maximum=64)
    if value.get("title") is not None:
        result["title"] = _text(value["title"], "room.title", maximum=256)
    if not result:
        raise ValidationError("room must contain id or title")
    return result


def _vitals(value: Any) -> dict[str, dict[str, int]] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("vitals must be an object or null")
    _strict_keys(
        value,
        allowed={"health", "mana", "spirit", "stamina"},
        required=set(),
    )
    result: dict[str, dict[str, int]] = {}
    for name, pair in value.items():
        if not isinstance(pair, Mapping):
            raise ValidationError(f"vitals.{name} must be an object")
        _strict_keys(pair, allowed={"current", "max"}, required={"current", "max"})
        current = _integer(
            pair["current"],
            f"vitals.{name}.current",
            minimum=-1_000_000,
            maximum=1_000_000,
        )
        maximum = _integer(pair["max"], f"vitals.{name}.max", maximum=1_000_000)
        result[name] = {"current": current, "max": maximum}
    return result


def _hands(value: Any) -> dict[str, dict[str, str] | None] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("hands must be an object or null")
    _strict_keys(value, allowed={"right", "left"}, required=set())
    result: dict[str, dict[str, str] | None] = {}
    for side, item in value.items():
        if item is None:
            result[side] = None
            continue
        if not isinstance(item, Mapping):
            raise ValidationError(f"hands.{side} must be an object or null")
        _strict_keys(item, allowed={"id", "name"}, required={"id", "name"})
        object_id = item["id"]
        if not isinstance(object_id, (str, int)) or isinstance(object_id, bool):
            raise ValidationError(f"hands.{side}.id must be a string or integer")
        result[side] = {
            "id": _text(str(object_id), f"hands.{side}.id", maximum=64),
            "name": _text(item["name"], f"hands.{side}.name", maximum=256),
        }
    return result


def _spells(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_STATE_ITEMS:
        raise ValidationError("active_spells must be a bounded array or null")
    result: list[dict[str, Any]] = []
    for index, spell in enumerate(value):
        if not isinstance(spell, Mapping):
            raise ValidationError(f"active_spells[{index}] must be an object")
        _strict_keys(
            spell,
            allowed={"id", "remaining_seconds"},
            required={"id"},
        )
        spell_id = spell["id"]
        if not isinstance(spell_id, (str, int)) or isinstance(spell_id, bool):
            raise ValidationError(f"active_spells[{index}].id must be a string or integer")
        item: dict[str, Any] = {
            "id": _text(str(spell_id), f"active_spells[{index}].id", maximum=32)
        }
        if spell.get("remaining_seconds") is not None:
            item["remaining_seconds"] = _number(
                spell["remaining_seconds"],
                f"active_spells[{index}].remaining_seconds",
                maximum=31_536_000,
            )
        result.append(item)
    return result


def _character_data(value: Any) -> dict[str, Any] | None:
    """Validate observed INFO/SKILLS separately from timestamp-unknown caches."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("character_data must be an object or null")
    _strict_keys(value, allowed={"info", "skills"}, required=set())
    result: dict[str, Any] = {}
    for category, item in value.items():
        if not isinstance(item, Mapping):
            raise ValidationError(f"character_data.{category} must be an object")
        _strict_keys(item, allowed={"source", "observed_at", "complete", "observed_level", "values"},
                     required={"source", "observed_at", "complete", "values"})
        source = item["source"]
        if not isinstance(source, str) or source not in {category, "infomon_cache"}:
            raise ValidationError(f"character_data.{category}.source is invalid")
        if not isinstance(item["complete"], bool):
            raise ValidationError(f"character_data.{category}.complete must be a boolean")
        observed_at = None if item["observed_at"] is None else _timestamp(item["observed_at"])
        if source == "infomon_cache" and (observed_at is not None or item["complete"]):
            raise ValidationError("infomon_cache has unknown observation time and is not complete")
        if item["complete"] and observed_at is None:
            raise ValidationError("complete character_data requires observed_at")
        values = item["values"]
        if not isinstance(values, Mapping):
            raise ValidationError(f"character_data.{category}.values must be an object")
        normalized: dict[str, Any] = {}
        if category == "info":
            _strict_keys(values, allowed={"level", "profession", "race", "gender", "age", "experience", "stats"}, required=set())
            for field in ("level", "age", "experience"):
                if values.get(field) is not None:
                    normalized[field] = _integer(values[field], field, maximum=100 if field == "level" else MAX_SEQUENCE)
            for field in ("profession", "race", "gender"):
                if values.get(field) is not None:
                    normalized[field] = _text(values[field], field, maximum=100)
            if "stats" in values:
                stats = values["stats"]
                if not isinstance(stats, Mapping):
                    raise ValidationError("character_data.info.stats must be an object")
                _strict_keys(stats, allowed={"STR", "CON", "DEX", "AGI", "DIS", "AUR", "LOG", "INT", "WIS", "INF"}, required=set())
                normalized["stats"] = {}
                for code, numbers in stats.items():
                    if not isinstance(numbers, Mapping):
                        raise ValidationError(f"stat {code} must be an object")
                    _strict_keys(numbers, allowed={"value", "bonus", "base_value", "base_bonus", "enhanced_value", "enhanced_bonus"}, required=set())
                    normalized["stats"][code] = {
                        field: _integer(number, f"stats.{code}.{field}", minimum=-1_000, maximum=1_000)
                        for field, number in numbers.items() if number is not None
                    }
        else:
            _strict_keys(values, allowed={"skills", "spell_circles", "training_points"}, required=set())
            for group, limit in (("skills", 64), ("spell_circles", 32)):
                if group not in values:
                    continue
                entries = values[group]
                if not isinstance(entries, Mapping) or len(entries) > limit:
                    raise ValidationError(f"character_data.{group} must contain at most {limit} entries")
                normalized[group] = {}
                for name, entry in entries.items():
                    key = _text(name, group, maximum=80)
                    if group == "spell_circles":
                        normalized[group][key] = _integer(entry, f"{group}.{key}", maximum=10_000)
                    else:
                        if not isinstance(entry, Mapping):
                            raise ValidationError(f"skill {key} must be an object")
                        _strict_keys(entry, allowed={"ranks", "bonus"}, required={"ranks"})
                        normalized[group][key] = {
                            field: _integer(number, f"{group}.{key}.{field}", maximum=10_000)
                            for field, number in entry.items() if number is not None
                        }
                        if "ranks" not in normalized[group][key]:
                            raise ValidationError(f"skill {key} ranks must be known, including zero")
            if "training_points" in values:
                points = values["training_points"]
                if not isinstance(points, Mapping):
                    raise ValidationError("training_points must be an object")
                _strict_keys(points, allowed={"physical", "mental"}, required=set())
                normalized["training_points"] = {
                    name: _integer(number, f"training_points.{name}", maximum=MAX_SEQUENCE)
                    for name, number in points.items() if number is not None
                }
        result[category] = {
            "source": source, "observed_at": observed_at, "complete": item["complete"],
            "observed_level": None if item.get("observed_level") is None else _integer(item["observed_level"], "observed_level", maximum=100),
            "values": normalized,
        }
    return result


def _nearby(value: Any) -> dict[str, list[dict[str, str]]] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("nearby must be an object or null")
    _strict_keys(value, allowed={"creatures", "corpses"}, required=set())
    result: dict[str, list[dict[str, str]]] = {}
    for group, raw_items in value.items():
        if not isinstance(raw_items, list) or len(raw_items) > MAX_STATE_ITEMS:
            raise ValidationError(f"nearby.{group} must be a bounded array")
        items: list[dict[str, str]] = []
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, Mapping):
                raise ValidationError(f"nearby.{group}[{index}] must be an object")
            _strict_keys(
                raw_item,
                allowed={"id", "noun", "name"},
                required={"id", "noun"},
            )
            object_id = raw_item["id"]
            if not isinstance(object_id, (str, int)) or isinstance(object_id, bool):
                raise ValidationError(
                    f"nearby.{group}[{index}].id must be a string or integer"
                )
            item = {
                "id": _text(
                    str(object_id), f"nearby.{group}[{index}].id", maximum=64
                ),
                "noun": _text(
                    raw_item["noun"],
                    f"nearby.{group}[{index}].noun",
                    maximum=128,
                ),
            }
            if raw_item.get("name") is not None:
                item["name"] = _text(
                    raw_item["name"],
                    f"nearby.{group}[{index}].name",
                    maximum=256,
                )
            items.append(item)
        result[group] = items
    return result


def _scripts(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_STATE_ITEMS:
        raise ValidationError("scripts must be a bounded array or null")
    return [
        _text(item, f"scripts[{index}]", maximum=128)
        for index, item in enumerate(value)
    ]


def _owners(value: Any) -> dict[str, str | None] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("owners must be an object or null")
    _strict_keys(
        value,
        allowed={"movement", "combat", "inventory", "communication"},
        required=set(),
    )
    return {
        lane: _optional_text(owner, f"owners.{lane}", maximum=128)
        for lane, owner in value.items()
    }


def _script_status(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("script_status must be an object or null")
    if len(value) > MAX_JSON_KEYS:
        raise ValidationError("script_status has too many fields")
    result: dict[str, str] = {}
    for name, status in value.items():
        normalized_name = _text(name, "script_status key", maximum=128)
        result[normalized_name] = _text(
            status, f"script_status.{normalized_name}", maximum=64
        )
    return result


def _string_or_integer(value: Any, field: str) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValidationError(f"{field} must be a string, integer, or null")
    if isinstance(value, str):
        return _text(value, field, maximum=MAX_STATE_TEXT_LENGTH)
    return _integer(value, field, maximum=1_000_000)


@dataclass(frozen=True, slots=True)
class CharacterSnapshot:
    """One bounded full snapshot from one Lich session generation.

    Unknown live values are represented by omitted optional fields or ``null``;
    unknown schema fields are rejected so producer drift fails closed.
    """

    character: str
    generation: str
    sequence: int
    observed_at: str
    game: str = "GSIV"
    session_id: str | None = None
    room: dict[str, str] | None = None
    vitals: dict[str, dict[str, int]] | None = None
    stance: str | None = None
    roundtime: float | None = None
    stunned: bool | None = None
    dead: bool | None = None
    mind: str | int | None = None
    encumbrance: str | int | None = None
    hands: dict[str, dict[str, str] | None] | None = None
    wounds: dict[str, Any] | None = None
    active_spells: list[dict[str, Any]] | None = None
    nearby: dict[str, list[dict[str, str]]] | None = None
    scripts: list[str] | None = None
    owners: dict[str, str | None] | None = None
    script_status: dict[str, str] | None = None
    character_data: dict[str, Any] | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CharacterSnapshot":
        if not isinstance(value, Mapping):
            raise ValidationError("snapshot must be an object")
        allowed = {
            "character",
            "generation",
            "sequence",
            "observed_at",
            "game",
            "session_id",
            "room",
            "vitals",
            "stance",
            "roundtime",
            "stunned",
            "dead",
            "mind",
            "encumbrance",
            "hands",
            "wounds",
            "active_spells",
            "nearby",
            "scripts",
            "owners",
            "script_status",
            "character_data",
        }
        _strict_keys(
            value,
            allowed=allowed,
            required={"character", "generation", "sequence", "observed_at"},
        )
        for field in ("stunned", "dead"):
            if value.get(field) is not None and not isinstance(value[field], bool):
                raise ValidationError(f"{field} must be a boolean or null")
        wounds = value.get("wounds")
        if wounds is not None and not isinstance(wounds, Mapping):
            raise ValidationError("wounds must be an object or null")
        return cls(
            character=_text(
                value["character"], "character", maximum=MAX_CHARACTER_LENGTH
            ),
            generation=_text(
                value["generation"], "generation", maximum=MAX_GENERATION_LENGTH
            ),
            sequence=_integer(value["sequence"], "sequence"),
            observed_at=_timestamp(value["observed_at"]),
            game=_text(value.get("game", "GSIV"), "game", maximum=16),
            session_id=_optional_text(
                value.get("session_id"), "session_id", maximum=MAX_SESSION_ID_LENGTH
            ),
            room=_room(value.get("room")),
            vitals=_vitals(value.get("vitals")),
            stance=_optional_text(value.get("stance"), "stance", maximum=64),
            roundtime=(
                None
                if value.get("roundtime") is None
                else _number(value["roundtime"], "roundtime", maximum=3_600)
            ),
            stunned=value.get("stunned"),
            dead=value.get("dead"),
            mind=_string_or_integer(value.get("mind"), "mind"),
            encumbrance=_string_or_integer(value.get("encumbrance"), "encumbrance"),
            hands=_hands(value.get("hands")),
            wounds=(
                None
                if wounds is None
                else _bounded_json(wounds, "wounds")
            ),
            active_spells=_spells(value.get("active_spells")),
            nearby=_nearby(value.get("nearby")),
            scripts=_scripts(value.get("scripts")),
            owners=_owners(value.get("owners")),
            script_status=_script_status(value.get("script_status")),
            character_data=_character_data(value.get("character_data")),
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "character": self.character,
            "generation": self.generation,
            "sequence": self.sequence,
            "observed_at": self.observed_at,
            "game": self.game,
        }
        for name in (
            "session_id",
            "room",
            "vitals",
            "stance",
            "roundtime",
            "stunned",
            "dead",
            "mind",
            "encumbrance",
            "hands",
            "wounds",
            "active_spells",
            "nearby",
            "scripts",
            "owners",
            "script_status",
            "character_data",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = _bounded_json(value, name)
        return result


@dataclass(frozen=True, slots=True)
class MeaningfulEvent:
    """A bounded explicit state change emitted by the active Lich session."""

    character: str
    generation: str
    observed_at: str
    kind: str
    summary: str
    data: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MeaningfulEvent":
        if not isinstance(value, Mapping):
            raise ValidationError("event must be an object")
        _strict_keys(
            value,
            allowed={"character", "generation", "observed_at", "kind", "summary", "data"},
            required={"character", "generation", "observed_at", "kind", "summary"},
        )
        data = value.get("data", {})
        if not isinstance(data, Mapping):
            raise ValidationError("data must be an object")
        return cls(
            character=_text(value["character"], "character", maximum=MAX_CHARACTER_LENGTH),
            generation=_text(value["generation"], "generation", maximum=MAX_GENERATION_LENGTH),
            observed_at=_timestamp(value["observed_at"]),
            kind=_text(value["kind"], "kind", maximum=64),
            summary=_text(value["summary"], "summary", maximum=MAX_EVENT_TEXT_LENGTH),
            data=_bounded_json(data, "data"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "character": self.character,
            "generation": self.generation,
            "observed_at": self.observed_at,
            "kind": self.kind,
            "summary": self.summary,
            "data": _bounded_json(self.data, "data"),
        }


@dataclass(frozen=True, slots=True)
class Observation:
    timestamp: str
    character: str
    text: str
    game: str = "GSIV"
    room_id: str | None = None
    source: str = "game"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Observation":
        if not isinstance(value, Mapping):
            raise ValidationError("each observation must be an object")
        _strict_keys(
            value,
            allowed={"timestamp", "character", "text", "game", "room_id", "source"},
            required={"timestamp", "character", "text"},
        )
        room_id = value.get("room_id")
        if room_id is not None and not isinstance(room_id, (str, int)):
            raise ValidationError("room_id must be a string, integer, or null")
        return cls(
            timestamp=_text(value["timestamp"], "timestamp", maximum=64),
            character=_text(
                value["character"], "character", maximum=MAX_CHARACTER_LENGTH
            ),
            text=_text(value["text"], "text", maximum=MAX_EVENT_TEXT_LENGTH),
            game=_text(value.get("game", "GSIV"), "game", maximum=16),
            room_id=None if room_id is None else str(room_id),
            source=_text(value.get("source", "game"), "source", maximum=32),
        )


@dataclass(frozen=True, slots=True)
class AskRequest:
    character: str
    question: str
    read_only: bool = False
    expected_generation: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AskRequest":
        if not isinstance(value, Mapping):
            raise ValidationError("ask request must be an object")
        _strict_keys(
            value,
            allowed={"character", "question", "read_only", "expected_generation"},
            required={"character", "question"},
        )
        read_only = value.get("read_only", False)
        if type(read_only) is not bool:
            raise ValidationError("read_only must be a boolean")
        generation = value.get("expected_generation")
        if "expected_generation" in value:
            generation = _text(generation, "expected_generation", maximum=MAX_GENERATION_LENGTH)
        return cls(
            character=_text(
                value["character"], "character", maximum=MAX_CHARACTER_LENGTH
            ),
            question=_text(value["question"], "question", maximum=MAX_QUESTION_LENGTH),
            read_only=read_only,
            expected_generation=generation,
        )


@dataclass(frozen=True, slots=True)
class CharacterRequest:
    """A strict character-only request for private per-character controls."""

    character: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CharacterRequest":
        if not isinstance(value, Mapping):
            raise ValidationError("character request must be an object")
        _strict_keys(value, allowed={"character"}, required={"character"})
        return cls(
            character=_text(
                value["character"], "character", maximum=MAX_CHARACTER_LENGTH
            )
        )


@dataclass(frozen=True, slots=True)
class Answer:
    request_id: str
    character: str
    text: str
    observed_event_count: int
    sources: tuple[Mapping[str, Any], ...] = ()
    source_diagnostics: tuple[Mapping[str, Any], ...] = ()
    capability: str = "read_only"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "character": self.character,
            "text": self.text,
            "observed_event_count": self.observed_event_count,
            "capability": self.capability,
            "sources": [dict(item) for item in self.sources],
            "source_diagnostics": [dict(item) for item in self.source_diagnostics],
        }
