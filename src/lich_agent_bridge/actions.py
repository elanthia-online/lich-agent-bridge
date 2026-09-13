"""Fail-closed command proposals for the local Lich bridge.

The broker is the policy seam.  HTTP and Lich adapters only translate messages;
all command classification, confirmation, expiry, character/room binding, and
state transitions live here.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .controller_manifest import ControllerManifest
from .errors import ConfigurationError, ValidationError

MAX_COMMAND_LENGTH = 160
MAX_SEQUENCE_COMMANDS = 12
MAX_SEQUENCE_LENGTH = 1200
MAX_DETAIL_LENGTH = 500
MAX_TTL_SECONDS = 120
DEFAULT_TTL_SECONDS = 45


def _strict_keys(
    value: Mapping[str, Any], *, allowed: set[str], required: set[str]
) -> None:
    if not isinstance(value, Mapping):
        raise ValidationError("action request must be an object")
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


def _character(value: Any) -> str:
    return _text(value, "character", maximum=40)


def _room(value: Any) -> str:
    if not isinstance(value, (str, int)):
        raise ValidationError("room_id must be a string or integer")
    return _text(str(value), "room_id", maximum=80)


def _action_id(value: Any) -> str:
    action_id = _text(value, "action_id", maximum=64)
    if not re.fullmatch(r"[0-9a-f]{16}", action_id):
        raise ValidationError("action_id is invalid")
    return action_id


def _generation(value: Any) -> str:
    return _text(value, "generation", maximum=128)


@dataclass(frozen=True, slots=True)
class ActionProposal:
    character: str
    command: str | None = None
    commands: tuple[str, ...] = ()
    expected_room_id: str | None = None
    expected_generation: str | None = None
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    # Internal owner deadline; the HTTP proposal schema deliberately omits it.
    deadline: float | None = None
    # Exact owning operation deadline for opted-in controlled launches only.
    # Unlike expires_at, this remains relevant after dispatch.
    controller_deadline: float | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionProposal":
        _strict_keys(
            value,
            allowed={
                "character",
                "command",
                "commands",
                "expected_room_id",
                "expected_generation",
                "ttl_seconds",
            },
            required={"character"},
        )
        has_command = "command" in value
        has_commands = "commands" in value
        if has_command == has_commands:
            raise ValidationError("provide exactly one of command or commands")
        parsed_commands: tuple[str, ...] = ()
        parsed_command: str | None = None
        if has_command:
            parsed_command = _text(
                value["command"], "command", maximum=MAX_COMMAND_LENGTH
            )
        else:
            raw_commands = value["commands"]
            if not isinstance(raw_commands, list):
                raise ValidationError("commands must be an array")
            if not 2 <= len(raw_commands) <= MAX_SEQUENCE_COMMANDS:
                raise ValidationError(
                    f"commands must contain between 2 and {MAX_SEQUENCE_COMMANDS} entries"
                )
            parsed_commands = tuple(
                _text(command, f"commands[{index}]", maximum=MAX_COMMAND_LENGTH)
                for index, command in enumerate(raw_commands)
            )
            if sum(len(command) for command in parsed_commands) > MAX_SEQUENCE_LENGTH:
                raise ValidationError(
                    f"commands exceed {MAX_SEQUENCE_LENGTH} total characters"
                )
        ttl = value.get("ttl_seconds", DEFAULT_TTL_SECONDS)
        if isinstance(ttl, bool) or not isinstance(ttl, int):
            raise ValidationError("ttl_seconds must be an integer")
        if not 1 <= ttl <= MAX_TTL_SECONDS:
            raise ValidationError(
                f"ttl_seconds must be between 1 and {MAX_TTL_SECONDS}"
            )
        room = value.get("expected_room_id")
        generation = value.get("expected_generation")
        return cls(
            character=_character(value["character"]),
            command=parsed_command,
            commands=parsed_commands,
            expected_room_id=None if room is None else _room(room),
            expected_generation=(
                None if generation is None else _generation(generation)
            ),
            ttl_seconds=ttl,
        )


@dataclass(frozen=True, slots=True)
class ActionContext:
    character: str
    room_id: str
    generation: str = "generation-1"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionContext":
        _strict_keys(
            value,
            allowed={"character", "room_id", "generation"},
            required={"character", "room_id", "generation"},
        )
        return cls(
            character=_character(value["character"]),
            room_id=_room(value["room_id"]),
            generation=_generation(value["generation"]),
        )


@dataclass(frozen=True, slots=True)
class ActionNextRequest:
    """One bounded blocking-delivery request from the active Lich bridge."""

    context: ActionContext
    timeout_seconds: float = 15.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionNextRequest":
        _strict_keys(
            value,
            allowed={"character", "room_id", "generation", "timeout_seconds"},
            required={"character", "room_id", "generation"},
        )
        timeout = value.get("timeout_seconds", 15.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValidationError("timeout_seconds must be a number")
        if not 0 <= timeout <= 30:
            raise ValidationError("timeout_seconds must be between 0 and 30")
        return cls(
            context=ActionContext(
                character=_character(value["character"]),
                room_id=_room(value["room_id"]),
                generation=_generation(value["generation"]),
            ),
            timeout_seconds=float(timeout),
        )


@dataclass(frozen=True, slots=True)
class ActionStatusRequest:
    """Read one broker-owned action without advancing its lifecycle."""

    action_id: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionStatusRequest":
        _strict_keys(value, allowed={"action_id"}, required={"action_id"})
        return cls(action_id=_action_id(value["action_id"]))


@dataclass(frozen=True, slots=True)
class ActionApproval:
    action_id: str
    character: str
    room_id: str
    approval_mode: str = "manual"
    generation: str = "generation-1"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionApproval":
        _strict_keys(
            value,
            allowed={"action_id", "character", "room_id", "generation", "approval_mode"},
            required={"action_id", "character", "room_id", "generation"},
        )
        approval_mode = _text(
            value.get("approval_mode", "manual"),
            "approval_mode",
            maximum=16,
        ).casefold()
        if approval_mode not in {"manual", "auto"}:
            raise ValidationError("approval_mode must be manual or auto")
        return cls(
            action_id=_action_id(value["action_id"]),
            character=_character(value["character"]),
            room_id=_room(value["room_id"]),
            generation=_generation(value["generation"]),
            approval_mode=approval_mode,
        )


@dataclass(frozen=True, slots=True)
class ActionControl:
    character: str
    enabled: bool
    generation: str = "generation-1"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionControl":
        _strict_keys(
            value,
            allowed={"character", "enabled", "generation"},
            required={"character", "enabled", "generation"},
        )
        if not isinstance(value["enabled"], bool):
            raise ValidationError("enabled must be a boolean")
        return cls(
            character=_character(value["character"]),
            enabled=value["enabled"],
            generation=_generation(value["generation"]),
        )


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: str
    character: str
    outcome: str
    detail: str
    generation: str = "generation-1"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionResult":
        _strict_keys(
            value,
            allowed={"action_id", "character", "generation", "outcome", "detail"},
            required={"action_id", "character", "generation", "outcome", "detail"},
        )
        outcome = _text(value["outcome"], "outcome", maximum=16).casefold()
        if outcome not in {"completed", "failed", "sent_unverified", "succeeded"}:
            raise ValidationError(
                "outcome must be completed, failed, sent_unverified, or succeeded"
            )
        detail = value["detail"]
        if not isinstance(detail, str):
            raise ValidationError("detail must be a string")
        detail = detail.replace("\x00", "").strip()[:MAX_DETAIL_LENGTH]
        return cls(
            action_id=_action_id(value["action_id"]),
            character=_character(value["character"]),
            generation=_generation(value["generation"]),
            outcome=outcome,
            detail=detail,
        )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    kind: str
    confirmation_required: bool


class CommandPolicy:
    """Classify the explicit supervised-driver command vocabulary."""

    _IMMEDIATE = frozenset(
        {
            "armor info",
            "armor list all",
            "ascension info",
            "bounty",
            "cman info",
            "encumbrance",
            "exp",
            "feat info",
            "gld",
            "health",
            "info",
            "inspect ready weapon links",
            "inventory",
            "look",
            "mana",
            "ready list",
            "ready weapon set",
            "resource",
            "skills",
            "shield info",
            "society",
            "spell active",
            "spirit",
            "stamina",
            "stow list",
            "weapon info",
        }
    )
    _CONFIGURATION = (
        re.compile(r"\Aready weapon clear\Z"),
        re.compile(r"\Aready weapon #[0-9]+\Z"),
        re.compile(r"\Aready weapon set #[0-9]+\Z"),
        re.compile(r"\A(?:mark|register) [^\r\n]{1,130}\Z"),
        re.compile(r"\Aset (?:nomarkeddrop|saferdrop) on\Z"),
        re.compile(r"\Ago2 getsilvers (?:on|off)\Z"),
        re.compile(r"\Aeloot keep closed on\Z"),
        re.compile(r"\Aeloot (?:sell|stop)\Z"),
        re.compile(r"\Aeherbs heal\Z"),
        re.compile(r"\Abigshot (?:start|stop)\Z"),
        re.compile(r"\Aebounty (?:start|stop)\Z"),
        re.compile(r"\A(?:flag|stow set|store set) [^\r\n]{1,130}\Z"),
    )
    _MOVEMENT = (
        re.compile(r"\A(?:north|northeast|east|southeast|south|southwest|west|northwest|out|up|down|n|ne|e|se|s|sw|w|nw|u|d)\Z"),
        re.compile(r"\A(?:go|climb|enter) [a-z0-9][a-z0-9 #'_-]{0,119}\Z"),
        re.compile(r"\Ago2 [0-9]+\Z"),
        re.compile(r"\Ago2 supervised [1-9][0-9]{0,9}\Z"),
    )
    _COMMUNICATION = re.compile(r"\A(?:say|whisper|tell|ask) [^\r\n]{1,140}\Z")
    _LOCAL_INSPECTION = re.compile(
        r"\A(?:inspect item exact [^\r\n]{1,120}|"
        r"diagnose item (?:405|735) exact [^\r\n]{1,120})\Z"
    )
    _MIRACLE_TELEPORT = re.compile(r"\Abeseech teleport\Z")
    _SESSION_RECOVERY = re.compile(r"\Alab recover [0-9a-f]{16} confirm\Z")
    _DIRECT_COMMAND = re.compile(r"\Alab direct ([^\r\n]{1,140})\Z", re.IGNORECASE)
    _ELOOT_CURRENT_ROOM = re.compile(r"\Aeloot loot\Z")
    _READ_ONLY = re.compile(
        r"\A(?:look|read|inspect|analyze|assess|appraise|browse|shop)(?:\s+[^\r\n]{1,130})?\Z"
    )
    _INVENTORY = re.compile(
        r"\A(?:get|take|remove|wear|stow|sheath|gird|open|close) [^\r\n]{1,130}\Z|"
        r"\Aput [^\r\n]{1,90} (?:in|into|on|under|behind) [^\r\n]{1,90}\Z"
    )
    _COMMERCE = re.compile(
        r"\A(?:buy|order)\Z|"
        r"\A(?:buy|order|bid|deposit|withdraw|tip) [^\r\n]{1,130}\Z"
    )
    _SCROLL_INFUSION = (
        re.compile(
            r"\Apour (?:#[0-9]+|my (?:[a-z0-9][a-z0-9 #'_-]{0,84} )?potion) on "
            r"(?:#[0-9]+|my (?:[a-z0-9][a-z0-9 #'_-]{0,84} )?(?:stone|runestone))\Z"
        ),
        re.compile(
            r"\Adip (?:#[0-9]+|my (?:[a-z0-9][a-z0-9 #'_-]{0,84} )?brush) in "
            r"(?:#[0-9]+|my (?:[a-z0-9][a-z0-9 #'_-]{0,84} )?(?:ink|cup))\Z"
        ),
        re.compile(r"\Adraw [a-z]{1,20}'[a-z]{1,20} rune\Z"),
        re.compile(
            r"\Awave (?:#[0-9]+|my (?:[a-z0-9][a-z0-9 #'_-]{0,84} )?"
            r"(?:stone|runestone)) at (?:#[0-9]+|my [a-z0-9][a-z0-9 #'_-]{0,110})\Z"
        ),
        re.compile(r"\A(?:prep|prepare) 714\Z"),
        re.compile(r"\Acast at #[0-9]+\Z"),
        re.compile(r"\Ainfuse (?:#[0-9]+|my [a-z0-9][a-z0-9 #'_-]{0,110})\Z"),
    )
    _COMBAT = re.compile(
        r"\A(?:attack|kill|berserk|mstrike|stance|retreat|advance|cman|warcry|sign|incant|cast|evoke|ambush|punch|kick|jab|grapple)(?:\s+[^\r\n]{1,130})?\Z"
    )
    _FORBIDDEN = (
        re.compile(
            r"\A(?:drop|discard|trash|sell|give|offer|exchange|trade|mail|place|throw|hurl|empty|destroy|sacrifice)(?:\s|\Z)"
        ),
        re.compile(r"\A(?:unmark(?:\s|\Z)|mark .+ remove\Z)"),
        re.compile(r"\Aset (?:nomarkeddrop|saferdrop) off\Z"),
        re.compile(r"\Aput .+ (?:on|in|into) (?:the )?(?:ground|floor|room)\Z"),
    )

    def __init__(self, controller_manifest: ControllerManifest | None = None):
        self._controller_manifest = controller_manifest or ControllerManifest.load()

    def is_test_launch(self, command: str) -> bool:
        matched = self._controller_manifest.match_command(command)
        return matched is not None and matched.controller.test_suite is not None

    def is_controller_control(self, command: str) -> bool:
        matched = self._controller_manifest.match_command(command)
        return matched is not None and matched.action.kind == "control"

    def is_controlled_launch(self, command: str) -> bool:
        matched = self._controller_manifest.match_command(command)
        return (matched is not None and matched.action.kind == "launch"
                and bool(matched.controller.control_owner_scripts))

    def is_refuge_launch(self, command: str) -> bool:
        matched = self._controller_manifest.match_command(command)
        return (matched is not None and matched.action.kind == "launch"
                and bool(matched.controller.control_owner_scripts)
                and matched.controller.safe_handoff["kind"] in {"quick_refuge", "controller_refuge"})

    def evaluate(self, command: str) -> tuple[str, PolicyDecision]:
        if any(character in command for character in ("\r", "\n", ";", "|", "&")):
            raise ValidationError("command chaining or control characters are forbidden")
        collapsed = " ".join(command.split())
        folded = collapsed.casefold()
        direct = self._DIRECT_COMMAND.fullmatch(collapsed)
        if direct is not None:
            inner = direct.group(1).strip()
            if not inner or inner.startswith((",", ";")):
                raise ValidationError("direct game command cannot invoke a client command")
            return f"lab direct {inner}", PolicyDecision("direct", False)
        if folded.startswith(",") or any(
            pattern.search(folded) for pattern in self._FORBIDDEN
        ):
            raise ValidationError("destructive item command is forbidden")
        if folded in self._IMMEDIATE:
            return folded, PolicyDecision("inspection", False)
        controller_command = self._controller_manifest.match_command(collapsed)
        if controller_command is not None:
            canonical, _, _ = controller_command.action.build(
                controller_command.arguments
            )
            return canonical, PolicyDecision(
                controller_command.action.category,
                controller_command.action.confirmation_required,
            )
        if self._ELOOT_CURRENT_ROOM.fullmatch(folded):
            return folded, PolicyDecision("inventory", True)
        if self._LOCAL_INSPECTION.fullmatch(folded):
            return collapsed, PolicyDecision("inspection", False)
        if self._READ_ONLY.fullmatch(folded):
            return collapsed, PolicyDecision("inspection", False)
        if self._SESSION_RECOVERY.fullmatch(folded):
            return folded, PolicyDecision("configuration", True)
        if any(pattern.fullmatch(folded) for pattern in self._CONFIGURATION):
            return folded, PolicyDecision("configuration", True)
        if any(pattern.fullmatch(folded) for pattern in self._MOVEMENT):
            return folded, PolicyDecision("movement", True)
        if self._COMMUNICATION.fullmatch(folded):
            return collapsed, PolicyDecision("communication", True)
        if self._MIRACLE_TELEPORT.fullmatch(folded):
            return folded, PolicyDecision("combat", True)
        if self._INVENTORY.fullmatch(folded):
            return collapsed, PolicyDecision("inventory", True)
        if self._COMMERCE.fullmatch(folded):
            return collapsed, PolicyDecision("commerce", True)
        if any(pattern.fullmatch(folded) for pattern in self._SCROLL_INFUSION):
            return collapsed, PolicyDecision("crafting", True)
        if self._COMBAT.fullmatch(folded):
            return collapsed, PolicyDecision("combat", True)
        raise ValidationError("command is not in the supervised-driver allowlist")


@dataclass(slots=True)
class _Action:
    action_id: str
    character: str
    generation: str
    commands: tuple[str, ...]
    kind: str
    status: str
    expected_room_id: str | None
    created_at: float
    expires_at: float
    confirmation_required: bool
    notified: bool = False
    completion: str | None = None
    detail: str | None = None
    test_run: bool = False
    controller_control: bool = False
    controller_deadline: float | None = None
    stop_requested: bool = False
    return_requested: bool = False

    @property
    def travel_run(self) -> bool:
        return len(self.commands) == 1 and re.fullmatch(r"go2 supervised [1-9][0-9]{0,9}", self.commands[0], re.IGNORECASE) is not None

    def public(self, *, instruction: str) -> dict[str, Any]:
        result = {
            "action_id": self.action_id,
            "character": self.character,
            "generation": self.generation,
            "kind": self.kind,
            "status": instruction,
            "expected_room_id": self.expected_room_id,
            "expires_at": self.expires_at,
        }
        if len(self.commands) == 1:
            result["command"] = self.commands[0]
        else:
            result["commands"] = list(self.commands)
            result["step_count"] = len(self.commands)
        if self.completion is not None:
            result["completion"] = self.completion
        if self.detail is not None:
            result["detail"] = self.detail
        if self.controller_deadline is not None:
            result["controller_deadline"] = self.controller_deadline
            result["return_requested"] = self.return_requested
        if self.test_run or self.controller_control or self.controller_deadline is not None or self.travel_run:
            result["stop_requested"] = self.stop_requested
        return result


class JsonlAuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def __call__(self, event: Mapping[str, Any]) -> None:
        record = {"timestamp": time.time(), **event}
        encoded = json.dumps(record, separators=(",", ":"), sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")


class ActionBroker:
    """Own the complete lifecycle for one-at-a-time, character-bound actions."""

    def __init__(
        self,
        *,
        policy: CommandPolicy | None = None,
        audit: Callable[[Mapping[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._policy = policy or CommandPolicy()
        self._audit = audit or (lambda _event: None)
        self._clock = clock
        self._enabled: set[str] = set()
        self._active_generations: dict[str, str] = {}
        self._actions: dict[str, _Action] = {}
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)

    def admit_generation(self, character: str, generation: str) -> dict[str, Any]:
        """Make a snapshot-admitted session generation authoritative.

        The caller invokes this only after WorldState accepted the snapshot.
        Pending actions from a replaced session are never deliverable to the
        new bridge.
        """

        display_name = _character(character)
        admitted_generation = _generation(generation)
        character_key = display_name.casefold()
        with self._lock:
            previous = self._active_generations.get(character_key)
            self._active_generations[character_key] = admitted_generation
            cancelled: list[str] = []
            if previous is not None and previous != admitted_generation:
                for action in self._actions.values():
                    if ((action.test_run or action.controller_control or action.controller_deadline is not None or action.travel_run)
                            and action.character.casefold() == character_key
                            and action.generation != admitted_generation
                            and action.status in {"dispatched", "completed", "failed"}):
                        action.stop_requested = True
                    if (
                        action.character.casefold() == character_key
                        and action.generation != admitted_generation
                        and action.status in {"confirmation_required", "queued"}
                    ):
                        action.status = "cancelled"
                        cancelled.append(action.action_id)
                self._audit(
                    {
                        "event": "generation_replaced",
                        "character": display_name,
                        "previous_generation": previous,
                        "generation": admitted_generation,
                        "cancelled_action_ids": cancelled,
                    }
                )
            self._changed.notify_all()
            return {
                "character": display_name,
                "generation": admitted_generation,
                "replaced_generation": previous if previous != admitted_generation else None,
                "cancelled_action_ids": cancelled,
            }

    def control(self, request: ActionControl) -> dict[str, Any]:
        character_key = request.character.casefold()
        with self._lock:
            self._require_active_generation_locked(
                request.character, request.generation
            )
            event = {
                "event": "execution_enabled" if request.enabled else "kill_switch",
                "character": request.character,
                "generation": request.generation,
            }
            self._audit(event)
            if request.enabled:
                self._enabled.add(character_key)
            else:
                self._enabled.discard(character_key)
                for action in self._actions.values():
                    if ((action.test_run or action.controller_control or action.controller_deadline is not None or action.travel_run)
                            and action.character.casefold() == character_key
                            and action.status in {"dispatched", "completed", "failed"}):
                        action.stop_requested = True
                    if (
                        action.character.casefold() == character_key
                        and action.status in {"confirmation_required", "queued"}
                    ):
                        action.status = "cancelled"
            self._changed.notify_all()
            return {
                "character": request.character,
                "generation": request.generation,
                "enabled": request.enabled,
            }

    def submit(self, proposal: ActionProposal) -> dict[str, Any]:
        normalized_commands, kind, confirmation_required = self._evaluate_proposal(
            proposal
        )
        with self._lock:
            character_key = proposal.character.casefold()
            active_generation = self._active_generations.get(character_key)
            if active_generation is None:
                raise ValidationError(
                    f"active session generation is unavailable for {proposal.character}"
                )
            if (
                proposal.expected_generation is not None
                and proposal.expected_generation != active_generation
            ):
                raise ValidationError("proposal generation does not match active session")
            if character_key not in self._enabled:
                raise ValidationError(f"actions are disabled for {proposal.character}")
            now = self._clock()
            controlled_launch = len(normalized_commands) == 1 and self._policy.is_controlled_launch(normalized_commands[0])
            if controlled_launch:
                if (isinstance(proposal.controller_deadline, bool)
                        or not isinstance(proposal.controller_deadline, (int, float))
                        or not math.isfinite(proposal.controller_deadline)
                        or proposal.controller_deadline <= now
                        or proposal.expected_generation is None):
                    raise ValidationError("controlled launch requires a finite future operation deadline and generation")
            elif proposal.controller_deadline is not None:
                raise ValidationError("controller deadline is only valid for a registered controlled launch")
            expires_at = now + proposal.ttl_seconds
            if proposal.deadline is not None:
                if (isinstance(proposal.deadline, bool) or not isinstance(proposal.deadline, (int, float))
                        or not math.isfinite(proposal.deadline) or proposal.deadline <= now):
                    raise ValidationError("operation deadline expired or invalid before action admission")
                expires_at = min(expires_at, proposal.deadline)
            action = _Action(
                action_id=secrets.token_hex(8),
                character=proposal.character,
                generation=active_generation,
                commands=normalized_commands,
                kind=kind,
                status=("confirmation_required" if confirmation_required else "queued"),
                expected_room_id=proposal.expected_room_id,
                created_at=now,
                expires_at=expires_at,
                confirmation_required=confirmation_required,
                test_run=(len(normalized_commands) == 1 and self._policy.is_test_launch(normalized_commands[0])),
                controller_control=(len(normalized_commands) == 1 and self._policy.is_controller_control(normalized_commands[0])),
                controller_deadline=proposal.controller_deadline if controlled_launch else None,
            )
            self._audit(
                {
                    "event": "action_proposed",
                    "action_id": action.action_id,
                    "character": action.character,
                    "generation": action.generation,
                    **self._command_audit_fields(action),
                    "kind": action.kind,
                    "status": action.status,
                    "expected_room_id": action.expected_room_id,
                    "expires_at": action.expires_at,
                    **({"controller_deadline": action.controller_deadline} if controlled_launch else {}),
                }
            )
            self._actions[action.action_id] = action
            self._changed.notify_all()
            return action.public(instruction=action.status)

    def _evaluate_proposal(
        self, proposal: ActionProposal
    ) -> tuple[tuple[str, ...], str, bool]:
        """Normalize one command or one explicit, locally executed sequence.

        Sequences are deliberately limited to non-movement utility work.  Each
        step passes the exact same command policy as an ordinary proposal, and
        the complete ordered list is confirmation-gated as one visible action.
        """

        has_command = proposal.command is not None
        has_commands = bool(proposal.commands)
        if has_command == has_commands:
            raise ValidationError("provide exactly one of command or commands")
        if has_command:
            normalized, decision = self._policy.evaluate(
                _text(proposal.command, "command", maximum=MAX_COMMAND_LENGTH)
            )
            return (normalized,), decision.kind, decision.confirmation_required

        if not 2 <= len(proposal.commands) <= MAX_SEQUENCE_COMMANDS:
            raise ValidationError(
                f"commands must contain between 2 and {MAX_SEQUENCE_COMMANDS} entries"
            )
        normalized_commands: list[str] = []
        total_length = 0
        for index, command in enumerate(proposal.commands):
            checked = _text(
                command, f"commands[{index}]", maximum=MAX_COMMAND_LENGTH
            )
            normalized, decision = self._policy.evaluate(checked)
            if self._policy.is_controller_control(normalized):
                raise ValidationError("controller controls cannot be batched")
            if decision.kind not in {"inspection", "inventory", "crafting"}:
                raise ValidationError(
                    "command sequences are limited to inspection, inventory, and crafting"
                )
            normalized_commands.append(normalized)
            total_length += len(normalized)
        if total_length > MAX_SEQUENCE_LENGTH:
            raise ValidationError(
                f"commands exceed {MAX_SEQUENCE_LENGTH} total characters"
            )
        return tuple(normalized_commands), "sequence", True

    @staticmethod
    def _command_audit_fields(action: _Action) -> dict[str, Any]:
        if len(action.commands) == 1:
            return {"command": action.commands[0]}
        return {"commands": list(action.commands), "step_count": len(action.commands)}

    def poll(self, context: ActionContext) -> dict[str, Any] | None:
        with self._lock:
            return self._poll_locked(context)

    def poll_wait(
        self, context: ActionContext, *, timeout_seconds: float = 15.0
    ) -> dict[str, Any] | None:
        """Block until one instruction is ready or a bounded timeout expires."""

        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValidationError("timeout_seconds must be a number")
        if not 0 <= timeout_seconds <= 30:
            raise ValidationError("timeout_seconds must be between 0 and 30")
        deadline = time.monotonic() + float(timeout_seconds)
        with self._changed:
            while True:
                instruction = self._poll_locked(context)
                if instruction is not None:
                    return instruction
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._changed.wait(remaining)

    def approve(self, approval: ActionApproval) -> dict[str, Any]:
        with self._lock:
            self._expire_locked()
            action = self._find_locked(approval.action_id)
            if action.character.casefold() != approval.character.casefold():
                raise ValidationError("action belongs to another character")
            self._require_active_generation_locked(
                approval.character, approval.generation
            )
            if action.generation != approval.generation:
                raise ValidationError("action belongs to another session generation")
            if action.status != "confirmation_required":
                raise ValidationError("action is not awaiting confirmation")
            if (
                action.expected_room_id is not None
                and action.expected_room_id != approval.room_id
            ):
                action.status = "denied_stale_room"
                self._audit(
                    {
                        "event": "action_denied",
                        "reason": "stale_room_at_approval",
                        "action_id": action.action_id,
                        "character": action.character,
                        **self._command_audit_fields(action),
                    }
                )
                raise ValidationError("room changed before confirmation")
            action.expected_room_id = approval.room_id
            action.status = "queued"
            self._audit(
                {
                    "event": "action_approved",
                    "action_id": action.action_id,
                    "character": action.character,
                    "generation": action.generation,
                    **self._command_audit_fields(action),
                    "room_id": approval.room_id,
                    "approval_mode": approval.approval_mode,
                }
            )
            self._changed.notify_all()
            return action.public(instruction="queued")

    def record_result(self, result: ActionResult) -> dict[str, Any]:
        with self._lock:
            action = self._find_locked(result.action_id)
            if action.character.casefold() != result.character.casefold():
                raise ValidationError("action belongs to another character")
            self._require_active_generation_locked(
                result.character, result.generation
            )
            if action.generation != result.generation:
                raise ValidationError("action belongs to another session generation")
            if action.status != "dispatched":
                raise ValidationError("action was not dispatched")
            # ``completed`` is retained as the legacy bridge outcome, but it
            # means only that the command was sent.  Evidence-aware adapters
            # must report ``succeeded`` explicitly.  The public status remains
            # compatible for existing callers while ``completion`` tells the
            # truth needed by compound operations.
            if result.outcome == "failed":
                action.status = "failed"
                action.completion = "failed"
            elif result.outcome == "succeeded":
                action.status = "completed"
                action.completion = "succeeded"
            else:
                action.status = "completed"
                action.completion = "sent_unverified"
            action.detail = result.detail
            self._audit(
                {
                    "event": "action_result",
                    "action_id": action.action_id,
                    "character": action.character,
                    "generation": action.generation,
                    **self._command_audit_fields(action),
                    "outcome": result.outcome,
                    "completion": action.completion,
                    "detail": result.detail,
                }
            )
            self._changed.notify_all()
            return {
                "action_id": action.action_id,
                "generation": action.generation,
                "status": action.status,
                "completion": action.completion,
                "detail": action.detail,
            }

    def get(self, action_id: str) -> dict[str, Any]:
        """Return current broker-owned action state without changing authority."""

        with self._lock:
            self._expire_locked()
            action = self._find_locked(_action_id(action_id))
            return action.public(instruction=action.status)

    def cancel(self, action_id: str, *, character: str, generation: str) -> dict[str, Any]:
        """Revoke one caller-owned pending action, never an already sent command.

        Internal operation cleanup only: identity is checked against the action,
        not the current session, so old-generation owners can still clean up.
        Dispatched supervised go2 additionally receives a stop marker for its
        native guard; already-sent movement is never undone.
        """

        with self._lock:
            action = self._find_locked(_action_id(action_id))
            if action.character.casefold() != _character(character).casefold():
                raise ValidationError("action belongs to another character")
            if action.generation != _generation(generation):
                raise ValidationError("action belongs to another session generation")
            self._expire_locked()
            cancelled = action.status in {"confirmation_required", "queued"}
            if action.travel_run and action.status == "dispatched":
                action.stop_requested = True
                self._audit({"event": "travel_stop_requested", "action_id": action.action_id,
                             "character": action.character, "generation": action.generation})
                self._changed.notify_all()
            if cancelled:
                action.status = "cancelled"
                self._audit({
                    "event": "action_cancelled", "action_id": action.action_id,
                    "character": action.character, "generation": action.generation,
                    "reason": "owning_operation_cancelled",
                })
                self._changed.notify_all()
            return {**action.public(instruction=action.status), "cancelled": cancelled}

    def request_test_stop(self, action_id: str, *, character: str, generation: str) -> dict[str, Any]:
        """Internal exact-owner stop; dispatched work remains dispatched/completed."""
        with self._lock:
            action = self._find_locked(_action_id(action_id))
            if (action.character.casefold() != _character(character).casefold()
                    or action.generation != _generation(generation)):
                raise ValidationError("test action belongs to another character or generation")
            if not action.test_run:
                raise ValidationError("action is not a registered test launch")
            self._expire_locked()
            if action.status in {"confirmation_required", "queued"}:
                action.status = "cancelled"
            elif action.status in {"dispatched", "completed", "failed"}:
                action.stop_requested = True
            self._audit({"event": "test_stop_requested", "action_id": action.action_id,
                         "character": action.character, "generation": action.generation,
                         "status": action.status, "stop_requested": action.stop_requested})
            self._changed.notify_all()
            return action.public(instruction=action.status)

    def revoke_controller_control(self, action_id: str, *, character: str, generation: str) -> dict[str, Any]:
        """Revoke exact owned control; queued native application observes a stop marker.

        Dispatched controls cannot be unsent. The bridge's off-thread status
        observation and local queued validity predicate govern later application.
        """
        with self._lock:
            action = self._find_locked(_action_id(action_id))
            if (action.character.casefold() != _character(character).casefold()
                    or action.generation != _generation(generation) or not action.controller_control):
                raise ValidationError("control does not belong to this character and generation")
            self._expire_locked()
            if action.status in {"confirmation_required", "queued"}:
                action.status = "cancelled"
            action.stop_requested = True
            self._audit({"event": "controller_control_revoked", "action_id": action.action_id,
                         "character": action.character, "generation": action.generation,
                         "status": action.status})
            self._changed.notify_all()
            return action.public(instruction=action.status)

    def request_controller_return(self, action_id: str, *, character: str, generation: str) -> dict[str, Any]:
        """End exact refuge test work without revoking its already approved return."""
        with self._lock:
            action = self._find_locked(_action_id(action_id))
            if (action.character.casefold() != _character(character).casefold()
                    or action.generation != _generation(generation) or action.controller_deadline is None
                    or len(action.commands) != 1 or not self._policy.is_refuge_launch(action.commands[0])):
                raise ValidationError("return request requires this character's exact refuge launch")
            self._expire_locked()
            self._require_active_generation_locked(action.character, action.generation)
            if (action.stop_requested or action.status not in {"dispatched", "completed"}
                    or action.character.casefold() not in self._enabled
                    or self._clock() >= action.controller_deadline):
                raise ValidationError("refuge launch is no longer authorized")
            action.return_requested = True
            self._audit({"event": "controller_return_requested", "action_id": action.action_id,
                         "character": action.character, "generation": action.generation})
            self._changed.notify_all()
            return action.public(instruction=action.status)

    def revoke_controller_run(self, action_id: str, *, character: str, generation: str) -> dict[str, Any]:
        """Mark the exact controlled launch revoked; never kill a script by name."""
        with self._lock:
            action = self._find_locked(_action_id(action_id))
            if (action.character.casefold() != _character(character).casefold()
                    or action.generation != _generation(generation) or action.controller_deadline is None):
                raise ValidationError("controlled launch does not belong to this character and generation")
            self._expire_locked()
            if action.status in {"confirmation_required", "queued"}:
                action.status = "cancelled"
            action.stop_requested = True
            self._audit({"event": "controller_run_revoked", "action_id": action.action_id,
                         "character": action.character, "generation": action.generation,
                         "status": action.status})
            self._changed.notify_all()
            return action.public(instruction=action.status)

    def _poll_locked(self, context: ActionContext) -> dict[str, Any] | None:
        self._expire_locked()
        self._require_active_generation_locked(
            context.character, context.generation
        )
        if context.character.casefold() not in self._enabled:
            return None
        for action in self._actions.values():
            if action.character.casefold() != context.character.casefold():
                continue
            if action.generation != context.generation:
                continue
            if action.status == "confirmation_required" and not action.notified:
                action.notified = True
                self._audit(
                    {
                        "event": "confirmation_requested",
                        "action_id": action.action_id,
                        "character": action.character,
                        "generation": action.generation,
                        **self._command_audit_fields(action),
                    }
                )
                return action.public(instruction="confirmation_required")
            if action.status != "queued":
                continue
            if (
                action.expected_room_id is not None
                and action.expected_room_id != context.room_id
            ):
                action.status = "denied_stale_room"
                self._audit(
                    {
                        "event": "action_denied",
                        "reason": "stale_room",
                        "action_id": action.action_id,
                        "character": action.character,
                        "generation": action.generation,
                        **self._command_audit_fields(action),
                        "expected_room_id": action.expected_room_id,
                        "actual_room_id": context.room_id,
                    }
                )
                continue
            action.status = "dispatched"
            self._audit(
                {
                    "event": "action_dispatched",
                    "action_id": action.action_id,
                    "character": action.character,
                    "generation": action.generation,
                    **self._command_audit_fields(action),
                    "room_id": context.room_id,
                }
            )
            return action.public(instruction="execute")
        return None

    def _find_locked(self, action_id: str) -> _Action:
        action = self._actions.get(action_id)
        if action is None:
            raise ValidationError("action was not found")
        return action

    def _require_active_generation_locked(
        self, character: str, generation: str
    ) -> None:
        active = self._active_generations.get(character.casefold())
        if active is None:
            raise ValidationError(
                f"active session generation is unavailable for {character}"
            )
        if active != generation:
            raise ValidationError("session generation does not match active bridge")

    def _expire_locked(self) -> None:
        now = self._clock()
        for action in self._actions.values():
            if action.status in {"confirmation_required", "queued"} and now >= action.expires_at:
                action.status = "expired"
                self._audit(
                    {
                        "event": "action_expired",
                        "action_id": action.action_id,
                        "character": action.character,
                        **self._command_audit_fields(action),
                    }
                )


def default_state_directory() -> Path:
    """Compatibility wrapper around the central settings module."""

    from .settings import Settings

    return Settings.load().storage.state_directory


def action_token_path() -> Path:
    """Compatibility wrapper around the central settings module."""

    from .settings import Settings

    return Settings.load().storage.action_token_file


def audit_log_path() -> Path:
    """Compatibility wrapper around the central settings module."""

    from .settings import Settings

    return Settings.load().storage.audit_log


def load_or_create_action_token(path: Path | None = None) -> str:
    selected = path or action_token_path()
    selected.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            selected,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        token = secrets.token_hex(32)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
    try:
        file_stat = selected.stat()
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise ConfigurationError("action token file must not be group/world accessible")
        token = selected.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ConfigurationError(f"cannot read action token file: {selected}") from error
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ConfigurationError("action token file is invalid")
    return token
