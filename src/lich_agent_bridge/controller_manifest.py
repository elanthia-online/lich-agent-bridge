"""Strict controller registry shared conceptually with the Lich bridge."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .errors import ConfigurationError, ValidationError

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_GLOBAL = re.compile(r"^\$lab_[a-z0-9_]+$")
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_LANES = frozenset({"movement", "combat", "inventory", "communication"})
_KINDS = frozenset({"launch", "stop", "signal", "status", "sync", "control"})
CONTROLLER_CONTROLS = frozenset({"status", "hold", "resume", "retreat"})
_CATEGORIES = frozenset({"inspection", "configuration", "movement", "combat"})
_PARAMETER_TYPES = frozenset({"enum", "numeric", "flag_suffix", "action_id"})


def _strict(
    value: Any, *, label: str, allowed: set[str], required: set[str]
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{label} must be an object")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise ConfigurationError(
            f"{label} has unsupported field(s): {', '.join(sorted(unknown))}"
        )
    if missing:
        raise ConfigurationError(
            f"{label} is missing field(s): {', '.join(sorted(missing))}"
        )
    return value


def _name(value: Any, label: str) -> str:
    result = str(value)
    if _NAME.fullmatch(result) is None:
        raise ConfigurationError(f"{label} is invalid")
    return result


def _token(value: Any, label: str) -> str:
    result = str(value)
    if _TOKEN.fullmatch(result) is None:
        raise ConfigurationError(f"{label} is invalid")
    return result


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    result = str(value)
    if not allow_empty and not result.strip():
        raise ConfigurationError(f"{label} must not be blank")
    if len(result) > 500 or any(character in result for character in "\r\n;|&"):
        raise ConfigurationError(f"{label} is invalid")
    return result


def _string_array(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{label} must be a nonempty array")
    result = tuple(_token(item, label) for item in value)
    if len({item.casefold() for item in result}) != len(result):
        raise ConfigurationError(f"{label} contains duplicates")
    return result


@dataclass(frozen=True, slots=True)
class ControllerParameter:
    name: str
    type: str
    values: tuple[str, ...] = ()
    true_value: str = ""
    default: bool = False

    @classmethod
    def from_mapping(cls, raw: Any, label: str) -> "ControllerParameter":
        value = _strict(
            raw,
            label=label,
            allowed={"name", "type", "values", "true_value", "default"},
            required={"name", "type"},
        )
        name = _name(value["name"], f"{label}.name")
        parameter_type = str(value["type"])
        if parameter_type not in _PARAMETER_TYPES:
            raise ConfigurationError(f"{label}.type is unsupported")
        raw_values = value.get("values", [])
        if not isinstance(raw_values, list):
            raise ConfigurationError(f"{label}.values must be an array")
        values = tuple(_token(item, f"{label}.values") for item in raw_values)
        true_value = str(value.get("true_value", ""))
        default = value.get("default", False)
        if parameter_type == "enum":
            if not values or len({item.casefold() for item in values}) != len(values):
                raise ConfigurationError(f"{label}.values must be nonempty and unique")
        elif parameter_type in {"numeric", "action_id"} and values:
            raise ConfigurationError(f"{label} {parameter_type} parameters do not accept values")
        elif parameter_type == "flag_suffix":
            if not isinstance(default, bool):
                raise ConfigurationError(f"{label}.default must be boolean")
            if not true_value.startswith(" ") or any(
                character in true_value for character in "\r\n;|&"
            ):
                raise ConfigurationError(f"{label}.true_value is invalid")
        return cls(name, parameter_type, values, true_value, bool(default))

    @property
    def required(self) -> bool:
        return self.type != "flag_suffix"

    def regex_fragment(self) -> str:
        if self.type == "enum":
            return "|".join(re.escape(item) for item in self.values)
        if self.type == "numeric":
            return r"[0-9]+"
        if self.type == "action_id":
            return r"(?-i:[0-9a-f]{16})"
        return re.escape(self.true_value)

    def render(self, raw: object) -> str:
        if self.type == "action_id":
            if not isinstance(raw, str) or re.fullmatch(r"[0-9a-f]{16}", raw) is None:
                raise ValidationError(f"{self.name} must be a lowercase 16-digit action ID")
            return raw
        if self.type == "enum":
            candidate = str(raw)
            canonical = next(
                (item for item in self.values if item.casefold() == candidate.casefold()),
                None,
            )
            if canonical is None:
                raise ValidationError(
                    f"{self.name} must be one of {', '.join(self.values)}"
                )
            return canonical
        if self.type == "numeric":
            if isinstance(raw, bool):
                raise ValidationError(f"{self.name} must be numeric")
            candidate = str(raw).strip()
            if not candidate.isdigit():
                raise ValidationError(f"{self.name} must be numeric")
            return candidate
        selected = self.default if raw is None else raw
        if not isinstance(selected, bool):
            raise ValidationError(f"{self.name} must be boolean")
        return self.true_value if selected else ""

    def normalize_capture(self, raw: str | None) -> str | bool:
        if self.type == "flag_suffix":
            return raw is not None and raw != ""
        if self.type == "enum":
            return next(
                item for item in self.values if item.casefold() == str(raw).casefold()
            )
        return str(raw)

    def json_schema(self) -> dict[str, Any]:
        if self.type == "enum":
            return {"type": "string", "enum": list(self.values)}
        if self.type == "numeric":
            return {"type": "string", "pattern": "^[0-9]+$"}
        if self.type == "action_id":
            return {"type": "string", "pattern": "^[0-9a-f]{16}$"}
        return {"type": "boolean", "default": self.default}


@dataclass(frozen=True, slots=True)
class ControllerAction:
    name: str
    kind: str
    command_template: str
    script_args_template: str
    launch_mode: str | None
    category: str
    confirmation_required: bool
    parameters: tuple[ControllerParameter, ...]
    pattern: re.Pattern[str]

    @classmethod
    def from_mapping(cls, raw: Any, label: str) -> "ControllerAction":
        value = _strict(
            raw,
            label=label,
            allowed={
                "name",
                "kind",
                "command_template",
                "script_args_template",
                "launch_mode",
                "policy",
                "parameters",
            },
            required={
                "name",
                "kind",
                "command_template",
                "script_args_template",
                "policy",
                "parameters",
            },
        )
        name = _name(value["name"], f"{label}.name")
        kind = str(value["kind"])
        if kind not in _KINDS:
            raise ConfigurationError(f"{label}.kind is unsupported")
        command_template = _text(value["command_template"], f"{label}.command_template")
        script_args_template = _text(
            value["script_args_template"],
            f"{label}.script_args_template",
            allow_empty=True,
        )
        launch_mode = None if value.get("launch_mode") is None else str(value["launch_mode"])
        if kind in {"launch", "sync"} and launch_mode not in {"start", "run"}:
            raise ConfigurationError(f"{label}.launch_mode must be start or run")
        policy = _strict(
            value["policy"],
            label=f"{label}.policy",
            allowed={"category", "confirmation_required"},
            required={"category", "confirmation_required"},
        )
        category = str(policy["category"])
        confirmation = policy["confirmation_required"]
        if category not in _CATEGORIES or not isinstance(confirmation, bool):
            raise ConfigurationError(f"{label}.policy is invalid")
        raw_parameters = value["parameters"]
        if not isinstance(raw_parameters, list):
            raise ConfigurationError(f"{label}.parameters must be an array")
        parameters = tuple(
            ControllerParameter.from_mapping(item, f"{label}.parameters[{index}]")
            for index, item in enumerate(raw_parameters)
        )
        if kind == "control" and (
            name not in CONTROLLER_CONTROLS
            or script_args_template != ""
            or launch_mode is not None
            or len(parameters) != 1
            or parameters[0].name != "run_id"
            or parameters[0].type != "action_id"
            or category != ("inspection" if name == "status" else "combat")
            or (name != "status" and not confirmation)
        ):
            raise ConfigurationError(f"{label} has an invalid exact-run control contract")
        names = tuple(item.name for item in parameters)
        if len(set(names)) != len(names):
            raise ConfigurationError(f"{label}.parameters contains duplicates")
        command_names = tuple(sorted(_PLACEHOLDER.findall(command_template)))
        if command_names != tuple(sorted(names)):
            raise ConfigurationError(
                f"{label}.command_template placeholders do not match parameters"
            )
        if not set(_PLACEHOLDER.findall(script_args_template)).issubset(names):
            raise ConfigurationError(f"{label}.script_args_template has invalid placeholders")
        parameter_by_name = {item.name: item for item in parameters}
        pieces = re.split(r"(\{[a-z][a-z0-9_]*\})", command_template)
        regex_parts: list[str] = []
        for piece in pieces:
            match = re.fullmatch(r"\{([a-z][a-z0-9_]*)\}", piece)
            if match is None:
                regex_parts.append(re.escape(piece))
                continue
            parameter = parameter_by_name[match.group(1)]
            capture = f"(?P<{parameter.name}>{parameter.regex_fragment()})"
            regex_parts.append(f"(?:{capture})?" if parameter.type == "flag_suffix" else capture)
        pattern = re.compile(r"\A" + "".join(regex_parts) + r"\Z", re.IGNORECASE)
        return cls(
            name,
            kind,
            command_template,
            script_args_template,
            launch_mode,
            category,
            confirmation,
            parameters,
            pattern,
        )

    def build(self, arguments: Mapping[str, object]) -> tuple[str, str, dict[str, object]]:
        source = {str(key): value for key, value in arguments.items()}
        allowed = {item.name for item in self.parameters}
        required = {item.name for item in self.parameters if item.required}
        unknown = set(source) - allowed
        missing = required - set(source)
        if unknown:
            raise ValidationError(
                f"unsupported controller argument(s): {', '.join(sorted(unknown))}"
            )
        if missing:
            raise ValidationError(
                f"missing controller argument(s): {', '.join(sorted(missing))}"
            )
        rendered: dict[str, str] = {}
        normalized: dict[str, object] = {}
        for parameter in self.parameters:
            raw = source.get(parameter.name, parameter.default)
            rendered[parameter.name] = parameter.render(raw)
            normalized[parameter.name] = (
                bool(raw) if parameter.type == "flag_suffix" else rendered[parameter.name]
            )
        command = _PLACEHOLDER.sub(lambda found: rendered[found.group(1)], self.command_template)
        script_args = _PLACEHOLDER.sub(
            lambda found: rendered[found.group(1)], self.script_args_template
        )
        return command, script_args, normalized

    def match(self, command: str) -> dict[str, object] | None:
        found = self.pattern.fullmatch(command)
        if found is None:
            return None
        try:
            return {
                parameter.name: (
                    parameter.render(found.group(parameter.name))
                    if parameter.type == "action_id"
                    else parameter.normalize_capture(found.group(parameter.name))
                )
                for parameter in self.parameters
            }
        except ValidationError:
            return None

    def argument_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                parameter.name: parameter.json_schema()
                for parameter in self.parameters
            },
            "required": [
                parameter.name for parameter in self.parameters if parameter.required
            ],
            "additionalProperties": False,
        }


@dataclass(frozen=True, slots=True)
class ControllerDefinition:
    name: str
    script: str
    summary: str
    characters: tuple[str, ...]
    result_global: str
    signal_global: str | None
    lanes: frozenset[str]
    owner_scripts: tuple[str, ...]
    safe_handoff: Mapping[str, Any]
    capability_action: str
    actions: tuple[ControllerAction, ...]
    test_suite: Mapping[str, Any] | None = None
    control_owner_scripts: tuple[str, ...] = ()

    def action(self, name: str) -> ControllerAction:
        selected = next((item for item in self.actions if item.name == name), None)
        if selected is None:
            raise ValidationError(f"controller action was not found: {self.name}.{name}")
        return selected

    def available_for(self, character: str) -> bool:
        folded = character.casefold()
        return any(item.casefold() == folded for item in self.characters)

    def safe_room(self, arguments: Mapping[str, object]) -> str | None:
        kind = self.safe_handoff["kind"]
        if kind in {"room", "quick_refuge", "controller_refuge"}:
            return str(self.safe_handoff["room_id"])
        if kind == "profile_room":
            profile = str(arguments.get("profile", ""))
            rooms = self.safe_handoff["rooms"]
            return str(rooms.get(profile)) if isinstance(rooms, Mapping) and profile in rooms else None
        return None


@dataclass(frozen=True, slots=True)
class ControllerCommand:
    controller: ControllerDefinition
    action: ControllerAction
    arguments: Mapping[str, object]
    script_args: str


class ControllerManifest:
    def __init__(self, controllers: tuple[ControllerDefinition, ...], path: Path):
        if len({item.name for item in controllers}) != len(controllers):
            raise ConfigurationError("controller names must be unique")
        if len({item.script for item in controllers}) != len(controllers):
            raise ConfigurationError("controller scripts must be unique")
        self.controllers = controllers
        self.path = path

    @classmethod
    def load(cls, path: Path | None = None) -> "ControllerManifest":
        selected = path or default_controller_manifest_path()
        try:
            raw = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigurationError(f"cannot load controller manifest: {error}") from error
        root = _strict(
            raw,
            label="manifest",
            allowed={"version", "controllers"},
            required={"version", "controllers"},
        )
        if root["version"] != 1:
            raise ConfigurationError("manifest.version must be 1")
        raw_controllers = root["controllers"]
        if not isinstance(raw_controllers, list):
            raise ConfigurationError("manifest.controllers must be an array")
        controllers = tuple(
            _controller_from_mapping(item, f"controllers[{index}]")
            for index, item in enumerate(raw_controllers)
        )
        return cls(controllers, selected)

    def controller(self, name: str) -> ControllerDefinition:
        selected = next(
            (item for item in self.controllers if item.name == name.casefold()), None
        )
        if selected is None:
            raise ValidationError(f"controller was not found: {name}")
        return selected

    def match_command(self, command: str) -> ControllerCommand | None:
        for controller in self.controllers:
            for action in controller.actions:
                arguments = action.match(command)
                if arguments is None:
                    continue
                _, script_args, normalized = action.build(arguments)
                return ControllerCommand(controller, action, normalized, script_args)
        return None


def _controller_from_mapping(raw: Any, label: str) -> ControllerDefinition:
    value = _strict(
        raw,
        label=label,
        allowed={
            "name",
            "script",
            "summary",
            "characters",
            "result_global",
            "signal_global",
            "lanes",
            "owner_scripts",
            "safe_handoff",
            "capability_action",
            "actions",
            "test_suite",
            "control_owner_scripts",
        },
        required={
            "name",
            "script",
            "summary",
            "characters",
            "result_global",
            "lanes",
            "owner_scripts",
            "safe_handoff",
            "capability_action",
            "actions",
        },
    )
    name = _name(value["name"], f"{label}.name")
    script = _name(value["script"], f"{label}.script")
    summary = _text(value["summary"], f"{label}.summary")
    characters = _string_array(value["characters"], f"{label}.characters")
    result_global = str(value["result_global"])
    signal_global = None if value.get("signal_global") is None else str(value["signal_global"])
    if _GLOBAL.fullmatch(result_global) is None or (
        signal_global is not None and _GLOBAL.fullmatch(signal_global) is None
    ):
        raise ConfigurationError(f"{label} has an invalid result/signal global")
    lanes = frozenset(_string_array(value["lanes"], f"{label}.lanes"))
    if not lanes.issubset(_LANES):
        raise ConfigurationError(f"{label}.lanes is invalid")
    owner_scripts = _string_array(value["owner_scripts"], f"{label}.owner_scripts")
    control_owners = (
        _string_array(value["control_owner_scripts"], f"{label}.control_owner_scripts")
        if "control_owner_scripts" in value else ()
    )
    safe = _strict(
        value["safe_handoff"],
        label=f"{label}.safe_handoff",
        allowed={"kind", "room_id", "rooms", "return_seconds"},
        required={"kind"},
    )
    kind = str(safe["kind"])
    refuge_kinds = {"quick_refuge", "controller_refuge"}
    if kind not in {"owners_released", "room", "profile_room", "quick_area", *refuge_kinds}:
        raise ConfigurationError(f"{label}.safe_handoff.kind is unsupported")
    if "return_seconds" in safe and kind not in refuge_kinds:
        raise ConfigurationError(f"{label}.return_seconds requires a refuge handoff")
    if kind in refuge_kinds:
        room = safe.get("room_id")
        seconds = safe.get("return_seconds")
        if (set(safe) != {"kind", "room_id", "return_seconds"}
                or isinstance(room, bool) or not re.fullmatch(r"[1-9][0-9]*", str(room))
                or str(room) == "4" or len(str(room)) > 12
                or type(seconds) is not int or not 10 <= seconds <= 120
                or not {"movement", "combat"}.issubset(lanes)):
            raise ConfigurationError(f"{label}.{kind} requires an exact refuge and 10–120 return seconds")
    if kind == "room" and not str(safe.get("room_id", "")).isdigit():
        raise ConfigurationError(f"{label}.safe_handoff.room_id must be numeric")
    if kind == "profile_room":
        rooms = safe.get("rooms")
        if not isinstance(rooms, Mapping) or not rooms:
            raise ConfigurationError(f"{label}.safe_handoff.rooms must be nonempty")
        if any(not _TOKEN.fullmatch(str(key)) or not str(room).isdigit() for key, room in rooms.items()):
            raise ConfigurationError(f"{label}.safe_handoff.rooms is invalid")
    raw_actions = value["actions"]
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ConfigurationError(f"{label}.actions must be a nonempty array")
    actions = tuple(
        ControllerAction.from_mapping(item, f"{label}.actions[{index}]")
        for index, item in enumerate(raw_actions)
    )
    if len({item.name for item in actions}) != len(actions):
        raise ConfigurationError(f"{label}.actions contains duplicates")
    capability_action = _name(value["capability_action"], f"{label}.capability_action")
    if not any(item.name == capability_action for item in actions):
        raise ConfigurationError(f"{label}.capability_action was not found")
    if any(item.kind == "control" for item in actions) and not any(
        item.name == capability_action and item.kind == "launch" for item in actions
    ):
        raise ConfigurationError(f"{label} controls require a launch capability")
    if any(item.kind == "control" for item in actions):
        if (script.casefold() not in {item.casefold() for item in control_owners}
                or not {item.casefold() for item in control_owners}.issubset(
                    item.casefold() for item in owner_scripts)):
            raise ConfigurationError(f"{label} requires explicit control_owner_scripts within owner_scripts")
    elif control_owners:
        raise ConfigurationError(f"{label}.control_owner_scripts requires registered controls")
    for launch in (item for item in actions if item.kind == "launch"):
        tokens = launch.script_args_template.split()
        mode = tokens[1] if len(tokens) >= 2 and tokens[0].casefold() == "quick" else ""
        may_seek = mode.casefold() == "seek" or any(
            mode == "{" + parameter.name + "}" and "seek" in {v.casefold() for v in parameter.values}
            for parameter in launch.parameters)
        if script == "bigshot" and may_seek and (
                kind not in {"quick_area", "quick_refuge"} or not {"movement", "combat"}.issubset(lanes)):
            raise ConfigurationError(f"{label}.seek requires quick_area/quick_refuge and movement/combat lanes")
    if kind in {"quick_area", "quick_refuge"}:
        launches = [item for item in actions if item.kind == "launch"]
        if ((kind == "quick_area" and set(safe) != {"kind"}) or script != "bigshot" or not control_owners
                or not launches):
            raise ConfigurationError(f"{label}.{kind} requires native controlled Bigshot without caller room lists")
        for launch in launches:
            tokens = launch.script_args_template.split()
            area_options = [index for index, token in enumerate(tokens) if token.startswith("--area")]
            if (not tokens or tokens[0] != "quick" or len(area_options) != 1
                    or tokens[area_options[0]:area_options[0] + 2] != ["--area", "profile"]
                    or any(parameter.type == "flag_suffix" for parameter in launch.parameters)
                    or any(token.startswith("--") and "{" in token for token in tokens)
                    or "--" in tokens):
                raise ConfigurationError(f"{label}.{kind} launches require explicit --area profile")
    if kind == "controller_refuge":
        launches = [item for item in actions if item.kind == "launch"]
        if not control_owners or not launches:
            raise ConfigurationError(f"{label}.controller_refuge requires a native controlled script")
        for launch in launches:
            tokens = launch.script_args_template.split()
            if (not tokens or any(parameter.type == "flag_suffix" for parameter in launch.parameters)
                    or any(token.startswith("--supervised-") for token in tokens)
                    or any(token.startswith("--") and "{" in token for token in tokens)
                    or "--" in tokens):
                raise ConfigurationError(
                    f"{label}.controller_refuge launches cannot supply private supervisor flags"
                )
    if any(item.kind == "signal" for item in actions) and signal_global is None:
        raise ConfigurationError(f"{label}.signal_global is required")
    controller = ControllerDefinition(
        name,
        script,
        summary,
        characters,
        result_global,
        signal_global,
        lanes,
        owner_scripts,
        dict(safe),
        capability_action,
        actions,
        _test_suite_metadata(value["test_suite"], label) if "test_suite" in value else None,
        control_owners,
    )
    if controller.test_suite is not None:
        _validate_test_controller(controller, label)
    return controller


def _test_suite_metadata(raw: Any, label: str) -> Mapping[str, Any]:
    value = _strict(raw, label=f"{label}.test_suite", allowed={"manifest", "files"},
                    required={"manifest", "files"})
    files = value["files"]
    if not isinstance(files, Mapping) or not 4 <= len(files) <= 67:
        raise ConfigurationError(f"{label}.test_suite.files must contain 4–67 pinned files")
    path_pattern = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*")
    for path, digest in files.items():
        if (not isinstance(path, str) or len(path) > 240 or path_pattern.fullmatch(path) is None
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise ConfigurationError(f"{label}.test_suite has an invalid relative path or SHA-256")
    manifest = value["manifest"]
    if (not isinstance(manifest, str) or not manifest.endswith(".json") or manifest not in files
            or not {"lab-test-runner.lic", "lab-test-runner.rb"}.issubset(files)
            or not any(path.endswith(".lic") and path != "lab-test-runner.lic" for path in files)):
        raise ConfigurationError(f"{label}.test_suite must pin its manifest, runner, and target")
    return {"manifest": manifest, "files": dict(files)}


def _validate_test_controller(controller: ControllerDefinition, label: str) -> None:
    suite = controller.test_suite
    assert suite is not None
    suite_id = controller.name.removeprefix("test-")
    if (not controller.name.startswith("test-") or _NAME.fullmatch(suite_id) is None
            or controller.script != "lab-test-runner" or len(controller.characters) != 1
            or controller.result_global != "$lab_test_result" or controller.signal_global != "$lab_test_cancel"
            or controller.lanes != {"movement", "combat"}
            or controller.safe_handoff["kind"] != "room"
            or "lab-test-runner" not in controller.owner_scripts
            or len(controller.actions) != 1 or controller.capability_action != "start"):
        raise ConfigurationError(f"{label} does not match the fixed test-runner contract")
    action = controller.action("start")
    params = {parameter.name: parameter for parameter in action.parameters}
    if (action.kind != "launch" or action.launch_mode != "start"
            or action.category != "configuration" or not action.confirmation_required
            or action.command_template != f"lab-test {suite_id} {{revision}} {{case_id}}"
            or action.script_args_template != f"{suite_id} {{revision}} {{case_id}}"
            or set(params) != {"revision", "case_id"}
            or params["revision"].type != "enum"
            or params["revision"].values != (suite["files"][suite["manifest"]],)
            or params["case_id"].type != "enum" or "all" not in params["case_id"].values
            or not 2 <= len(params["case_id"].values) <= 21):
        raise ConfigurationError(f"{label} has invalid test launch arguments or policy")


def default_controller_manifest_path() -> Path:
    """Compatibility wrapper around the central settings module."""

    from .settings import Settings

    return Settings.load().storage.controller_manifest
