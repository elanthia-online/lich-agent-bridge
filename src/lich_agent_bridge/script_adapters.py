"""Explicit registered script entrypoints for bounded capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class ScriptEntrypoint:
    name: str
    command: str
    numeric_argument: str | None = None

    def build(self, **arguments: object) -> str:
        if self.numeric_argument is None:
            if arguments:
                raise ValidationError(f"{self.name} does not accept arguments")
            return self.command
        if set(arguments) != {self.numeric_argument}:
            raise ValidationError(
                f"{self.name} requires only {self.numeric_argument}"
            )
        raw = arguments[self.numeric_argument]
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ValidationError(f"{self.numeric_argument} must be numeric")
        value = str(raw).strip()
        if not value.isdigit():
            raise ValidationError(f"{self.numeric_argument} must be numeric")
        return self.command.format(**{self.numeric_argument: value})


@dataclass(frozen=True, slots=True)
class ScriptAdapter:
    name: str
    ownership_lanes: frozenset[str]
    entrypoints: tuple[ScriptEntrypoint, ...]

    def command(self, entrypoint: str, **arguments: object) -> str:
        selected = next(
            (item for item in self.entrypoints if item.name == entrypoint), None
        )
        if selected is None:
            raise ValidationError(
                f"unsupported {self.name} entrypoint: {entrypoint}"
            )
        return selected.build(**arguments)


GO2_ADAPTER = ScriptAdapter(
    name="go2",
    ownership_lanes=frozenset({"movement"}),
    entrypoints=(
        ScriptEntrypoint(
            name="supervised_travel",
            command="go2 supervised {destination}",
            numeric_argument="destination",
        ),
        ScriptEntrypoint(
            name="travel",
            command="go2 {destination}",
            numeric_argument="destination",
        ),
    ),
)

BIGSHOT_ADAPTER = ScriptAdapter(
    name="bigshot",
    ownership_lanes=frozenset({"movement", "combat"}),
    entrypoints=(
        ScriptEntrypoint(name="start", command="bigshot start"),
        ScriptEntrypoint(name="stop", command="bigshot stop"),
    ),
)

ELOOT_ADAPTER = ScriptAdapter(
    name="eloot",
    ownership_lanes=frozenset({"inventory"}),
    entrypoints=(
        ScriptEntrypoint(name="loot_current_room", command="eloot loot"),
        ScriptEntrypoint(name="stop", command="eloot stop"),
    ),
)

SCRIPT_ADAPTERS: Mapping[str, ScriptAdapter] = MappingProxyType(
    {
        adapter.name: adapter
        for adapter in (GO2_ADAPTER, BIGSHOT_ADAPTER, ELOOT_ADAPTER)
    }
)


def script_adapter(name: str) -> ScriptAdapter:
    """Return one registered adapter; arbitrary script names fail closed."""

    if not isinstance(name, str):
        raise ValidationError("script adapter name must be a string")
    try:
        return SCRIPT_ADAPTERS[name.strip().casefold()]
    except KeyError as error:
        raise ValidationError(f"unregistered script adapter: {name}") from error
