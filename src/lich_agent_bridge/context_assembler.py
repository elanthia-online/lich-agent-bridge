"""Bounded structured context assembled from read-only local authorities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Callable, Mapping, Protocol, Sequence

from .character_knowledge import CharacterKnowledge, category_freshness
from .inventory import InventoryItem, InventoryKnowledge
from .knowledge import KnowledgeBase, KnowledgeExcerpt, KnowledgeSourceDiagnostic
from .protocol import MAX_STATE_ITEMS, Observation
from .world_state import WorldState


MAX_MEANINGFUL_EVENTS = 6
MAX_ACTIVE_ALERTS = 8
MAX_INVENTORY_ITEMS = 4
MAX_INVENTORY_FACTS = 4
MAX_KNOWLEDGE_EXCERPTS = 5
MAX_RAW_OBSERVATIONS = 24
MAX_PROMPT_CHARACTERS = 30_000

_WORD = re.compile(r"[a-z0-9][a-z0-9'-]*", re.IGNORECASE)
_OBJECT_ID = re.compile(r"#([0-9]+)")
_INVENTORY_HINTS = frozenset(
    {
        "armor",
        "container",
        "crown",
        "crystal",
        "dose",
        "dossier",
        "enhancive",
        "gear",
        "hand",
        "herb",
        "hood",
        "inventory",
        "item",
        "maul",
        "potion",
        "robe",
        "runestaff",
        "scroll",
        "staff",
        "sword",
        "weapon",
        "wearing",
    }
)
_QUERY_NOISE = frozenset(
    {
        "about",
        "does",
        "have",
        "inventory",
        "item",
        "know",
        "mine",
        "status",
        "that",
        "this",
        "what",
        "where",
        "which",
        "with",
    }
)


class ActiveAlertSource(Protocol):
    def active(self, character: str) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """Compact data records ready for safe prompt framing."""

    character: str
    question: str
    follow_up_question: str | None
    current_state: Mapping[str, Any] | None
    meaningful_events: tuple[Mapping[str, Any], ...]
    active_alerts: tuple[Mapping[str, Any], ...]
    inventory_items: tuple[Mapping[str, Any], ...]
    knowledge_excerpts: tuple[Mapping[str, Any], ...]
    knowledge_diagnostics: tuple[Mapping[str, Any], ...]
    raw_observations: tuple[Mapping[str, Any], ...]
    dialogue_history: tuple[Mapping[str, Any], ...] = ()
    character_data: tuple[Mapping[str, Any], ...] = ()
    max_prompt_characters: int = MAX_PROMPT_CHARACTERS

    def to_prompt(self) -> str:
        """Render source-labeled data blocks without granting instruction status."""
        return self.render()[0]

    def render(self) -> tuple[str, tuple[Mapping[str, Any], ...]]:
        """Return the prompt and references whose evidence actually fits in it.

        Records are admitted whole, so provenance cannot claim a source whose
        text was lost to a section or overall prompt boundary.
        """
        prefix = f"Character: {self.character}\nBEGIN UNTRUSTED CONTEXT DATA"
        footer = "\nEND UNTRUSTED CONTEXT DATA"
        omitted_marker = "\n[additional context omitted by prompt budget]"
        suffix_budget = self.max_prompt_characters - len(prefix) - len(footer) - len(omitted_marker)
        question_budget = max(1, min(2_000, self.max_prompt_characters // 3, suffix_budget - 19))
        question_suffix = f"\nPlayer question: {_short_text(self.question, question_budget)}"
        follow_up = ""
        if self.follow_up_question is not None:
            label = (
                "\nFollow-up context (immediately preceding question for this "
                "character): "
            )
            follow_up_budget = min(question_budget, suffix_budget - len(question_suffix) - len(label))
            if follow_up_budget > 0:
                follow_up = label + _short_text(self.follow_up_question, follow_up_budget)
        suffix = follow_up + question_suffix
        sections = [prefix]
        available = self.max_prompt_characters - len(prefix) - len(footer) - len(suffix) - len(omitted_marker)
        references: tuple[Mapping[str, Any], ...] = ()
        omitted = False
        blocks = (
            ("CURRENT LIVE STATE", self._optional(self.current_state), 12_000, False),
            ("CHARACTER BUILD", self.character_data, 9_000, False),
            ("REFERENCE KNOWLEDGE", self.knowledge_excerpts, 5_500, True),
            ("TEMPORARY DIALOGUE", self.dialogue_history, 6_000, False),
            ("ACTIVE WATCHER ALERTS", self.active_alerts, 2_500, False),
            ("RECENT MEANINGFUL EVENTS", self.meaningful_events, 3_500, False),
            ("DURABLE INVENTORY KNOWLEDGE", self.inventory_items, 4_000, False),
            ("REFERENCE SOURCE DIAGNOSTICS", self.knowledge_diagnostics, 800, False),
            ("RAW GAME OBSERVATIONS", self.raw_observations, 6_000, False),
        )
        for name, items, limit, fair_share in blocks:
            block, included = _render_section(
                name, items, min(limit, available - 1), fair_text_share=fair_share
            )
            if not block:
                omitted = omitted or bool(items)
                continue
            sections.append(block)
            available -= len(block) + 1
            if name == "REFERENCE KNOWLEDGE":
                references = tuple(
                    item for item in included
                    if isinstance(item.get("text"), str)
                    and item["text"].replace("…[truncated]", "").strip()
                )
        marker = omitted_marker if omitted else ""
        return "\n".join(sections) + marker + footer + suffix, references

    @staticmethod
    def _optional(value: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
        return () if value is None else (value,)


class ContextAssembler:
    """Compose optional structured sources with already-bounded raw observations."""

    def __init__(
        self,
        *,
        world_state: WorldState | None = None,
        watchers: ActiveAlertSource | None = None,
        knowledge: KnowledgeBase | None = None,
        inventory: InventoryKnowledge | None = None,
        character_knowledge: CharacterKnowledge | None = None,
        now: Callable[[], datetime] | None = None,
        max_meaningful_events: int = MAX_MEANINGFUL_EVENTS,
        max_active_alerts: int = MAX_ACTIVE_ALERTS,
        max_inventory_items: int = MAX_INVENTORY_ITEMS,
        max_knowledge_excerpts: int = MAX_KNOWLEDGE_EXCERPTS,
        max_raw_observations: int = MAX_RAW_OBSERVATIONS,
        max_prompt_characters: int = MAX_PROMPT_CHARACTERS,
    ):
        limits = {
            "max_meaningful_events": max_meaningful_events,
            "max_active_alerts": max_active_alerts,
            "max_inventory_items": max_inventory_items,
            "max_knowledge_excerpts": max_knowledge_excerpts,
            "max_raw_observations": max_raw_observations,
            "max_prompt_characters": max_prompt_characters,
        }
        for name, value in limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max_prompt_characters < 256:
            raise ValueError("max_prompt_characters must be at least 256")
        self._world_state = world_state
        self._watchers = watchers
        self._knowledge = knowledge
        self._inventory = inventory
        self._character_knowledge = character_knowledge
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._max_meaningful_events = max_meaningful_events
        self._max_active_alerts = max_active_alerts
        self._max_inventory_items = max_inventory_items
        self._max_knowledge_excerpts = max_knowledge_excerpts
        self._max_raw_observations = max_raw_observations
        self._max_prompt_characters = max_prompt_characters

    def build(
        self,
        *,
        character: str,
        question: str,
        observations: Sequence[Observation] = (),
        knowledge_question: str | None = None,
        follow_up_question: str | None = None,
        dialogue_history: Sequence[Mapping[str, Any]] = (),
        include_knowledge: bool = True,
    ) -> AssembledContext:
        selected_character = _short_text(character, 40)
        selected_question = _short_text(question, 2_000)
        current = self._current_state(selected_character)
        live_character_data = {} if current is None else current.pop("character_data", {})
        character_data = self._character_build(selected_character, current, live_character_data)
        generation = None if current is None else str(current["generation"])
        events = self._meaningful_events(selected_character, current)
        alerts = self._alerts(selected_character, generation)
        alerts = tuple(
            {**alert, "temporal_authority": "snapshot_at_question_start_not_answer_time",
             "snapshot_freshness": None if current is None else current["freshness"]}
            for alert in alerts
        )
        inventory = self._inventory_items(selected_character, selected_question)
        knowledge, diagnostics = (self._knowledge_items(
            selected_character,
            selected_question if knowledge_question is None else knowledge_question,
        ) if include_knowledge else ((), ()))
        raw = self._raw_observations(observations, selected_character)
        return AssembledContext(
            character=selected_character,
            question=selected_question,
            follow_up_question=(
                None
                if follow_up_question is None
                else _short_text(follow_up_question, 2_000)
            ),
            current_state=current,
            meaningful_events=events,
            active_alerts=alerts,
            inventory_items=inventory,
            knowledge_excerpts=knowledge,
            knowledge_diagnostics=diagnostics,
            raw_observations=raw,
            dialogue_history=tuple(_dialogue_record(item) for item in dialogue_history[-4:]),
            character_data=character_data,
            max_prompt_characters=self._max_prompt_characters,
        )

    def _current_state(self, character: str) -> dict[str, Any] | None:
        if self._world_state is None:
            return None
        current = self._world_state.snapshot(character)
        if current is None:
            return None
        snapshot = current["snapshot"]
        state: dict[str, Any] = {
            "authority": "lich_live_snapshot",
            "location_authority": (
                "last_observation_only" if current["freshness"].get("stale", True)
                else "current_live_state"
            ),
            "temporal_authority": "snapshot_at_question_start_not_answer_time",
            "character": snapshot["character"],
            "generation": snapshot["generation"],
            "sequence": snapshot["sequence"],
            "observed_at": snapshot["observed_at"],
            "freshness": _compact_value(current["freshness"]),
            "cursor": current["cursor"],
            "character_data": snapshot.get("character_data", {}),
        }
        for name in (
            "game",
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
            "owners",
        ):
            if name in snapshot:
                state[name] = _compact_value(snapshot[name])
        spells = snapshot.get("active_spells")
        if isinstance(spells, list):
            selected = spells[:MAX_STATE_ITEMS]
            # This protocol field is already bounded. Preserve every identity;
            # generic compaction truncates arrays and is inappropriate here.
            state["active_spell_ids"] = [str(item["id"]) for item in selected]
            state["active_spell_count"] = len(spells)
            state["active_spell_ids_complete"] = len(selected) == len(spells)
            state["active_spell_durations_seconds"] = {
                str(item["id"]): item["remaining_seconds"]
                for item in selected if item.get("remaining_seconds") is not None
            }
        else:
            state["active_spell_ids"] = None
            state["active_spell_count"] = None
            state["active_spell_ids_complete"] = False
        nearby = snapshot.get("nearby")
        if isinstance(nearby, Mapping):
            compact_nearby: dict[str, Any] = {}
            counts: dict[str, int] = {}
            for kind in ("creatures", "corpses"):
                values = nearby.get(kind)
                if not isinstance(values, list):
                    continue
                selected = sorted(
                    values,
                    key=lambda item: (
                        str(item.get("id", "")),
                        str(item.get("noun", "")),
                    ),
                )[:10]
                compact_nearby[kind] = _compact_value(selected)
                counts[kind] = len(values)
            state["nearby"] = compact_nearby
            state["nearby_counts"] = counts
        scripts = snapshot.get("scripts")
        if isinstance(scripts, list):
            state["scripts"] = sorted(str(item) for item in scripts)[:16]
            state["script_count"] = len(scripts)
        return state

    def _character_build(
        self, character: str, current: Mapping[str, Any] | None,
        live: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], ...]:
        generation = None if current is None else str(current["generation"])
        game = "GSIV" if current is None else str(current.get("game", "GSIV"))
        records = {} if self._character_knowledge is None else self._character_knowledge.find(
            character=character, game=game, generation=generation
        )
        current_level = live.get("info", {}).get("values", {}).get("level")
        now = self._now()
        selected = []
        for category in ("info", "skills"):
            prior = records.get(category)
            observed = live.get(category)
            if observed is not None:
                observed = {**observed, "character": character, "game": game,
                            "generation": generation, "category": category}
            # A completed observation can outlive a bridge cache reset within
            # the same generation, but never overrides a newer observed level.
            candidates = [item for item in (prior, observed) if item is not None]
            if not candidates:
                continue
            ranked = []
            for item in candidates:
                freshness = category_freshness(item, generation=generation, level=current_level, now=now)
                ranked.append((freshness["current"], item is observed, item, freshness))
            _, _, record, freshness = max(ranked, key=lambda item: item[:2])
            selected.append({
                **record, "freshness": freshness, "authority": freshness["authority"],
                "temporal_authority": "snapshot_at_question_start_not_answer_time",
                "precedence": "observed_values_before_historical_wiki; cached_values_have_unknown_age",
            })
        return tuple(selected)

    def _meaningful_events(
        self, character: str, current: Mapping[str, Any] | None
    ) -> tuple[Mapping[str, Any], ...]:
        if self._world_state is None or current is None:
            return ()
        cursor = int(current["cursor"])
        page = self._world_state.watch(
            character,
            cursor=max(0, cursor - self._max_meaningful_events),
            timeout=0,
        )
        generation = str(current["generation"])
        selected = [
            event
            for event in page["events"]
            if event.get("generation") == generation
        ][-self._max_meaningful_events :]
        return tuple(
            {
                "authority": "world_state_meaningful_event",
                "cursor": event["cursor"],
                "generation": event["generation"],
                "observed_at": event["observed_at"],
                "kind": event["kind"],
                "summary": _short_text(event["summary"], 500),
                "data": _compact_value(event.get("data", {})),
            }
            for event in selected
        )

    def _alerts(
        self, character: str, generation: str | None
    ) -> tuple[Mapping[str, Any], ...]:
        if self._watchers is None:
            return ()
        selected: list[Mapping[str, Any]] = []
        for alert in self._watchers.active(character):
            mapping = alert.to_mapping()
            if generation is not None and mapping.get("generation") != generation:
                continue
            selected.append(
                {
                    "authority": "deterministic_watcher",
                    "code": mapping.get("code"),
                    "severity": mapping.get("severity"),
                    "status": mapping.get("status"),
                    "generation": mapping.get("generation"),
                    "snapshot_sequence": mapping.get("snapshot_sequence"),
                    "evidence": _compact_value(mapping.get("evidence", {})),
                }
            )
        selected.sort(key=lambda item: (str(item["severity"]), str(item["code"])))
        return tuple(selected[: self._max_active_alerts])

    def _inventory_items(
        self, character: str, question: str
    ) -> tuple[Mapping[str, Any], ...]:
        if self._inventory is None:
            return ()
        query = _inventory_query(question)
        if query is None:
            return ()
        items = self._inventory.find(
            character=character,
            query=query,
            limit=self._max_inventory_items,
        )
        return tuple(self._inventory_mapping(item) for item in items)

    def _knowledge_items(
        self, character: str, question: str
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
        if self._knowledge is None:
            return (), ()
        result = self._knowledge.search(character=character, question=question)
        excerpts = getattr(result, "excerpts", tuple(result))
        diagnostics = getattr(result, "diagnostics", ())
        return (
            tuple(
                self._knowledge_mapping(excerpt)
                for excerpt in excerpts[: self._max_knowledge_excerpts]
            ),
            tuple(
                self._knowledge_diagnostic_mapping(diagnostic)
                for diagnostic in diagnostics
            ),
        )

    def _raw_observations(
        self, observations: Sequence[Observation], character: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        available = (
            observations
            if character is None
            else tuple(
                item
                for item in observations
                if item.character.casefold() == character.casefold()
            )
        )
        selected = available[-self._max_raw_observations :]
        return tuple(
            {
                "authority": "raw_game_observation",
                "source": item.source,
                "observed_at": item.timestamp,
                "room_id": item.room_id,
                "text": _short_text(item.text, 1_000),
            }
            for item in selected
        )

    @staticmethod
    def _inventory_mapping(item: InventoryItem) -> dict[str, Any]:
        identity = {
            "type": _short_text(item.item_type, 100),
            "noun": _short_text(item.noun, 100),
            "name": _short_text(item.name, 180),
            "full_name": _short_text(item.full_name, 240),
        }
        location = None
        if item.last_location is not None:
            location = {
                "authority": "last_observation_only",
                **{
                    key: _compact_value(item.last_location[key])
                    for key in (
                        "observed_at",
                        "kind",
                        "container_game_id",
                        "hand",
                        "game_id",
                    )
                    if key in item.last_location
                },
            }
        facts = []
        for fact in item.facts[:MAX_INVENTORY_FACTS]:
            facts.append(
                {
                    key: _compact_value(fact[key])
                    for key in (
                        "field",
                        "value",
                        "source",
                        "confidence",
                        "observed_at",
                    )
                    if key in fact
                }
            )
        return {
            "authority": "durable_inventory_knowledge",
            "current_location_authority": "none",
            "dossier_id": item.dossier_id,
            "fingerprint": _short_text(item.fingerprint, 240),
            "identity": identity,
            "last_seen_at": item.last_seen_at,
            "last_location": location,
            "facts": facts,
            "fact_count": len(item.facts),
        }

    @staticmethod
    def _knowledge_mapping(excerpt: KnowledgeExcerpt) -> dict[str, Any]:
        return {
            "content_role": "reference_data_not_instructions",
            "authority": excerpt.authority,
            "title": _short_text(excerpt.title, 240),
            "text": _short_text(excerpt.text, 1_400),
            "source": excerpt.source,
            "url": excerpt.url,
            "revision_id": excerpt.revision_id,
            "retrieved_at": excerpt.retrieved_at,
        }

    @staticmethod
    def _knowledge_diagnostic_mapping(
        diagnostic: KnowledgeSourceDiagnostic | Mapping[str, Any],
    ) -> dict[str, Any]:
        if isinstance(diagnostic, KnowledgeSourceDiagnostic):
            return diagnostic.to_mapping()
        return {
            "source": _short_text(diagnostic.get("source", "unknown"), 80),
            "status": _short_text(diagnostic.get("status", "unknown"), 40),
            "detail": _short_text(diagnostic.get("detail", ""), 300),
        }


def _inventory_query(question: str) -> str | None:
    object_id = _OBJECT_ID.search(question)
    if object_id is not None:
        return f"#{object_id.group(1)}"
    words = [word.casefold() for word in _WORD.findall(question)]
    if not _INVENTORY_HINTS.intersection(words):
        return None
    candidates = [word for word in words if len(word) > 2 and word not in _QUERY_NOISE]
    if not candidates:
        return "inventory"
    return max(enumerate(candidates), key=lambda item: (len(item[1]), -item[0]))[1]


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _short_text(value, 300)
    if depth >= 3:
        return "[nested data omitted]"
    if isinstance(value, Mapping):
        return {
            str(key): _compact_value(value[key], depth=depth + 1)
            for key in sorted(value, key=lambda item: str(item))[:12]
        }
    if isinstance(value, (list, tuple)):
        return [_compact_value(item, depth=depth + 1) for item in value[:16]]
    return _short_text(str(value), 300)


def _short_text(value: Any, maximum: int) -> str:
    text = str(value).replace("\x00", "").strip()
    if len(text) <= maximum:
        return text
    marker = " …[truncated]"
    if maximum <= len(marker):
        return text[:maximum]
    return text[: maximum - len(marker)].rstrip() + marker


def _dialogue_record(item: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "authority": "temporary_dialogue_not_verified_facts",
        "question": _short_text(item.get("question", ""), 1_000),
        "answer": _short_text(item.get("answer", ""), 2_000),
        "sources": [
            {key: _short_text(source[key], 150)
             for key in ("title", "source", "url", "revision_id")
             if source.get(key) is not None}
            for source in item.get("sources", ())[:3]
            if isinstance(source, Mapping)
        ],
    }


def _render_section(
    name: str,
    items: Sequence[Mapping[str, Any]],
    maximum: int,
    *,
    fair_text_share: bool = False,
) -> tuple[str, tuple[Mapping[str, Any], ...]]:
    header = f"BEGIN {name} DATA"
    footer = f"END {name} DATA"
    if maximum < len(header) + len(footer) + 2:
        return "", ()
    if not items:
        empty = f"{header}\n(no data)\n{footer}"
        return (empty, ()) if len(empty) <= maximum else ("", ())
    lines: list[str] = []
    included: list[Mapping[str, Any]] = []
    used = len(header) + len(footer) + 2
    for index, item in enumerate(items):
        record_maximum = maximum - used - 1
        if fair_text_share or name == "TEMPORARY DIALOGUE":
            remaining = len(items) - index
            record_maximum //= remaining
        line = _bounded_record(
            item,
            record_maximum,
            preserve_text=fair_text_share,
        )
        if used + len(line) + 1 > maximum:
            break
        lines.append(line)
        included.append(json.loads(line))
        used += len(line) + 1
    if len(lines) < len(items):
        marker = "[additional bounded data omitted]"
        if used + len(marker) + 1 <= maximum:
            lines.append(marker)
    return "\n".join((header, *lines, footer)), tuple(included)


def _bounded_record(
    item: Mapping[str, Any], maximum: int, *, preserve_text: bool = False
) -> str:
    def encode(value: Mapping[str, Any]) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    encoded = encode(item)
    if len(encoded) <= maximum:
        return encoded
    if "question" in item and "answer" in item:
        # Keep both sides of each turn when sharing the dialogue budget. Large
        # source metadata must not evict the answer a follow-up refers to.
        candidate = dict(item)
        candidate["sources"] = [
            {key: _short_text(source[key], 100)
             for key in ("title", "source") if key in source}
            for source in item.get("sources", ())
        ]
        for text_limit in (600, 400, 250, 100):
            candidate["question"] = _short_text(item["question"], text_limit)
            candidate["answer"] = _short_text(item["answer"], text_limit)
            encoded = encode(candidate)
            if len(encoded) <= maximum:
                return encoded
    text = item.get("text")
    if preserve_text and isinstance(text, str) and text:
        low = 1
        high = len(text)
        best: str | None = None
        while low <= high:
            midpoint = (low + high) // 2
            candidate = dict(item)
            candidate["text"] = _short_text(text, max(16, midpoint))
            candidate_encoded = encode(candidate)
            if len(candidate_encoded) <= maximum:
                best = candidate_encoded
                low = midpoint + 1
            else:
                high = midpoint - 1
        if best is not None:
            return best
    selected: dict[str, Any] = {}
    omitted: list[str] = []
    for key, value in item.items():
        candidate = {**selected, key: value}
        if len(encode(candidate)) <= max(2, maximum - 80):
            selected[key] = value
        else:
            omitted.append(str(key))
    selected["omitted_fields"] = omitted
    if "active_spell_ids" in omitted:
        # Absence from a bounded projection never proves an effect inactive.
        selected["active_spell_ids"] = None
        selected["active_spell_ids_complete"] = False
    encoded = encode(selected)
    if len(encoded) <= maximum:
        return encoded
    return encode(
        {
            "authority": item.get("authority", "bounded_context_record"),
            "record_omitted": "configured section boundary exceeded",
        }
    )
