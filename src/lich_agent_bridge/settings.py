"""Validated, secret-free configuration for Lich Agent Bridge.

This module is the configuration boundary for the application.  It deliberately
does not resolve credential values: provider settings contain only the name of
an environment variable that a runtime adapter may read later.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import ConfigurationError


SCHEMA_VERSION = 1
DEFAULT_PORT = 18_765
DEFAULT_MCP_PORT = 18_766
DEFAULT_OPENAI_MODEL = "gpt-5.6"
DEFAULT_EVIDENCE_RESULT_CHARS = 12_000
DEFAULT_EVIDENCE_TOTAL_CHARS = 36_000
_MAX_CONFIG_BYTES = 1_048_576
_MAX_DECISION_ENDPOINT_PATH_CHARS = 512
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh"}
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "selected_profile",
        "server",
        "knowledge",
        "storage",
        "decisions",
        "providers",
        "profiles",
    }
)
_DECISION_KEYS = frozenset(
    {
        "enabled",
        "kind",
        "base_url",
        "endpoint_path",
        "credential_env",
        "model",
        "timeout_seconds",
        "player_interval_seconds",
        "decision_ttl_seconds",
        "audit_log",
    }
)
_SERVER_KEYS = frozenset({"host", "port", "mcp_port"})
_KNOWLEDGE_KEYS = frozenset(
    {
        "project_root",
        "wiki_root",
        "gswiki_database",
        "semantic_model_directory",
        "mirror_max_age_hours",
        "online_fallback",
        "general_web_provider",
        "general_web_credential_env",
    }
)
_STORAGE_KEYS = frozenset(
    {
        "state_directory",
        "inventory_database",
        "lich_data_directory",
        "game",
        "action_token_file",
        "audit_log",
        "controller_manifest",
    }
)
_PROVIDER_KEYS = frozenset(
    {"kind", "command", "base_url", "credential_env"}
)
_PROFILE_KEYS = frozenset(
    {
        "provider",
        "model",
        "reasoning_effort",
        "timeout_seconds",
        "instructions_file",
        "web_search",
        "evidence_result_chars",
        "evidence_total_chars",
    }
)


class ProviderKind(StrEnum):
    """Wire contract implemented by a model provider adapter."""

    CODEX = "codex"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai_compatible"


class OnlineFallbackPolicy(StrEnum):
    """When authoritative live GSWiki may supplement local knowledge."""

    DISABLED = "disabled"
    WHEN_NEEDED = "when_needed"


class GeneralWebProvider(StrEnum):
    """Explicit general-web adapters; disabled is the safe default."""

    DISABLED = "disabled"
    BRAVE = "brave"


class DecisionProviderKind(StrEnum):
    """Wire contract implemented by the fast-decision provider."""

    SYSTEM_ONE = "system_one"


@dataclass(frozen=True, slots=True)
class ServerSettings:
    host: str
    port: int
    mcp_port: int


@dataclass(frozen=True, slots=True)
class KnowledgeSettings:
    project_root: Path
    wiki_root: Path
    gswiki_database: Path
    mirror_max_age_hours: float
    online_fallback: OnlineFallbackPolicy
    general_web_provider: GeneralWebProvider
    general_web_credential_env: str | None
    semantic_model_directory: Path | None = None


@dataclass(frozen=True, slots=True)
class StorageSettings:
    state_directory: Path
    inventory_database: Path | None
    lich_data_directory: Path | None
    game: str
    action_token_file: Path
    audit_log: Path
    controller_manifest: Path

    @property
    def timing_log(self) -> Path:
        """Return the existing TimingRecorder log beneath the shared state root."""

        return self.state_directory / "timings.jsonl"

    @property
    def resolved_inventory_database(self) -> Path | None:
        """Return the explicit database or the legacy Lich-data-derived path."""

        if self.inventory_database is not None:
            return self.inventory_database
        if self.lich_data_directory is None:
            return None
        return self.lich_data_directory / self.game / "lab-inventory.sqlite3"


@dataclass(frozen=True, slots=True)
class DecisionSettings:
    enabled: bool
    kind: DecisionProviderKind
    base_url: str
    endpoint_path: str
    credential_env: str | None
    model: str
    timeout_seconds: float
    player_interval_seconds: float
    decision_ttl_seconds: float
    audit_log: Path


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    kind: ProviderKind
    command: str | None
    base_url: str | None
    credential_env: str | None


@dataclass(frozen=True, slots=True)
class AgentProfile:
    provider: str
    model: str | None
    reasoning_effort: str | None
    timeout_seconds: float
    instructions_file: Path | None
    web_search: bool
    evidence_result_chars: int = DEFAULT_EVIDENCE_RESULT_CHARS
    evidence_total_chars: int = DEFAULT_EVIDENCE_TOTAL_CHARS


@dataclass(frozen=True, slots=True)
class Settings:
    """One immutable, fully validated runtime configuration."""

    path: Path
    schema_version: int
    selected_profile_name: str
    server: ServerSettings
    knowledge: KnowledgeSettings
    storage: StorageSettings
    decisions: DecisionSettings
    providers: Mapping[str, ProviderSettings]
    profiles: Mapping[str, AgentProfile]

    @classmethod
    def path_for(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> Path:
        """Resolve an explicit, ``LAB_CONFIG``, or XDG settings path."""

        env = os.environ if environment is None else environment
        selected: str | os.PathLike[str]
        if path is not None:
            selected = path
        elif env.get("LAB_CONFIG", "").strip():
            selected = env["LAB_CONFIG"]
        else:
            home = _home_directory(env)
            configured = env.get("XDG_CONFIG_HOME", "").strip()
            root = (
                _expand_path(configured, base=Path.cwd(), home=home)
                if configured
                else home / ".config"
            )
            selected = root / "lich-agent-bridge" / "config.toml"
        return _expand_path(selected, base=Path.cwd(), home=_home_directory(env))

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> "Settings":
        """Load settings using overrides > environment > TOML > defaults."""

        env = dict(os.environ if environment is None else environment)
        selected_path = cls.path_for(path, environment=env)
        raw = _default_mapping(environment=env)
        file_mapping = _read_toml(selected_path)
        if file_mapping is not None:
            _validate_schema_shape(file_mapping, source=str(selected_path))
            _merge(raw, file_mapping)
        _merge(raw, _environment_overrides(raw, env))
        if overrides is not None:
            if not isinstance(overrides, Mapping):
                raise ConfigurationError("settings overrides must be a mapping")
            copied = _copy_mapping(overrides, label="settings overrides")
            _validate_schema_shape(
                copied, source="settings overrides", require_version=False
            )
            _merge(raw, copied)
        return _build_settings(raw, path=selected_path, environment=env)

    @property
    def selected_profile(self) -> AgentProfile:
        return self.profiles[self.selected_profile_name]

    def redacted(self) -> dict[str, Any]:
        """Return a display-safe mapping.

        Settings never retain credential values, so the credential environment
        variable name can remain visible while its value cannot leak.
        """

        return self.to_mapping()

    def to_mapping(self) -> dict[str, Any]:
        """Return the strict schema as mutable, serialization-safe values."""

        return {
            "schema_version": self.schema_version,
            "selected_profile": self.selected_profile_name,
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "mcp_port": self.server.mcp_port,
            },
            "knowledge": {
                "project_root": str(self.knowledge.project_root),
                "wiki_root": str(self.knowledge.wiki_root),
                "gswiki_database": str(self.knowledge.gswiki_database),
                "semantic_model_directory": _path_text(self.knowledge.semantic_model_directory),
                "mirror_max_age_hours": self.knowledge.mirror_max_age_hours,
                "online_fallback": self.knowledge.online_fallback.value,
                "general_web_provider": self.knowledge.general_web_provider.value,
                "general_web_credential_env": self.knowledge.general_web_credential_env,
            },
            "storage": {
                "state_directory": str(self.storage.state_directory),
                "inventory_database": _path_text(self.storage.inventory_database),
                "lich_data_directory": _path_text(self.storage.lich_data_directory),
                "game": self.storage.game,
                "action_token_file": str(self.storage.action_token_file),
                "audit_log": str(self.storage.audit_log),
                "controller_manifest": str(self.storage.controller_manifest),
            },
            "decisions": {
                "enabled": self.decisions.enabled,
                "kind": self.decisions.kind.value,
                "base_url": self.decisions.base_url,
                "endpoint_path": self.decisions.endpoint_path,
                "credential_env": self.decisions.credential_env,
                "model": self.decisions.model,
                "timeout_seconds": self.decisions.timeout_seconds,
                "player_interval_seconds": self.decisions.player_interval_seconds,
                "decision_ttl_seconds": self.decisions.decision_ttl_seconds,
                "audit_log": str(self.decisions.audit_log),
            },
            "providers": {
                name: {
                    "kind": provider.kind.value,
                    "command": provider.command,
                    "base_url": provider.base_url,
                    "credential_env": provider.credential_env,
                }
                for name, provider in self.providers.items()
            },
            "profiles": {
                name: {
                    "provider": profile.provider,
                    "model": profile.model,
                    "reasoning_effort": profile.reasoning_effort,
                    "timeout_seconds": profile.timeout_seconds,
                    "instructions_file": _path_text(profile.instructions_file),
                    "web_search": profile.web_search,
                    "evidence_result_chars": profile.evidence_result_chars,
                    "evidence_total_chars": profile.evidence_total_chars,
                }
                for name, profile in self.profiles.items()
            },
        }

    def write(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        replace: bool = False,
    ) -> Path:
        """Atomically write this secret-free configuration.

        Existing files are preserved unless ``replace`` is explicitly true.
        """

        destination = (
            self.path
            if path is None
            else _expand_path(
                path,
                base=Path.cwd(),
                home=_home_directory(os.environ),
            )
        )
        if destination.exists() and not replace:
            raise ConfigurationError(
                f"settings file already exists: {destination}; pass replace=True to replace it"
            )
        writable = self.to_mapping()
        if (
            self.storage.action_token_file
            == self.storage.state_directory / "action-token"
        ):
            writable["storage"]["action_token_file"] = None
        if self.storage.audit_log == self.storage.state_directory / "actions.jsonl":
            writable["storage"]["audit_log"] = None
        if (
            self.decisions.audit_log
            == self.storage.state_directory / "decisions-shadow.jsonl"
        ):
            writable["decisions"]["audit_log"] = None
        if self.decisions.credential_env is None:
            # TOML has no null value.  An explicit empty string preserves a
            # credential-free local provider instead of restoring the hosted
            # default when this file is loaded again.
            writable["decisions"]["credential_env"] = ""
        payload = _toml_document(writable).encode("utf-8")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, destination)
            else:
                try:
                    os.link(temporary, destination)
                except FileExistsError as error:
                    raise ConfigurationError(
                        f"settings file already exists: {destination}; "
                        "pass replace=True to replace it"
                    ) from error
                temporary.unlink()
            _sync_directory(destination.parent)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise
        return destination


def _default_mapping(*, environment: Mapping[str, str]) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[2]
    home = _home_directory(environment)
    configured_state = environment.get("XDG_STATE_HOME", "").strip()
    state_root = (
        _expand_path(configured_state, base=Path.cwd(), home=home)
        if configured_state
        else home / ".local" / "state"
    )
    state_directory = state_root / "lich-agent-bridge"
    return {
        "schema_version": SCHEMA_VERSION,
        "selected_profile": "default",
        "server": {
            "host": "127.0.0.1",
            "port": DEFAULT_PORT,
            "mcp_port": DEFAULT_MCP_PORT,
        },
        "knowledge": {
            "project_root": str(project_root),
            "wiki_root": None,
            "gswiki_database": None,
            "semantic_model_directory": None,
            "mirror_max_age_hours": 168.0,
            "online_fallback": OnlineFallbackPolicy.WHEN_NEEDED.value,
            "general_web_provider": GeneralWebProvider.DISABLED.value,
            "general_web_credential_env": None,
        },
        "storage": {
            "state_directory": str(state_directory),
            "inventory_database": None,
            "lich_data_directory": None,
            "game": "GSIV",
            "action_token_file": None,
            "audit_log": None,
            "controller_manifest": None,
        },
        "decisions": {
            "enabled": False,
            "kind": DecisionProviderKind.SYSTEM_ONE.value,
            "base_url": "https://api.typesafe.ai",
            "endpoint_path": "/v1/systemone",
            "credential_env": "JEV_API_KEY",
            "model": "jev-1.13.0",
            "timeout_seconds": 2.0,
            "player_interval_seconds": 1.0,
            "decision_ttl_seconds": 1.0,
            "audit_log": None,
        },
        "providers": {
            "codex": {
                "kind": ProviderKind.CODEX.value,
                "command": "codex",
                "base_url": None,
                "credential_env": None,
            },
            "openai": {
                "kind": ProviderKind.OPENAI.value,
                "command": None,
                "base_url": "https://api.openai.com/v1",
                "credential_env": "OPENAI_API_KEY",
            },
            "llama_cpp": {
                "kind": ProviderKind.OPENAI_COMPATIBLE.value,
                "command": None,
                "base_url": "http://127.0.0.1:8080/v1",
                "credential_env": None,
            },
        },
        "profiles": {
            "default": {
                "provider": "codex",
                "model": None,
                "reasoning_effort": None,
                "timeout_seconds": 120.0,
                "instructions_file": None,
                "web_search": False,
                "evidence_result_chars": DEFAULT_EVIDENCE_RESULT_CHARS,
                "evidence_total_chars": DEFAULT_EVIDENCE_TOTAL_CHARS,
            }
        },
    }


def _read_toml(path: Path) -> dict[str, Any] | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigurationError(f"cannot read settings file {path}: {error}") from error
    if len(data) > _MAX_CONFIG_BYTES:
        raise ConfigurationError(
            f"settings file {path} exceeds {_MAX_CONFIG_BYTES} bytes"
        )
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"invalid TOML in settings file {path}: {error}") from error
    if not isinstance(parsed, dict):  # Defensive: tomllib currently always returns dict.
        raise ConfigurationError(f"settings file {path} must contain a TOML table")
    return parsed


def _environment_overrides(
    current: Mapping[str, Any], environment: Mapping[str, str]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    home = _home_directory(environment)

    def set_if(name: str, *keys: str, convert: Any = str) -> None:
        value = environment.get(name)
        if value is None or not value.strip():
            return
        try:
            converted = convert(value.strip())
        except (TypeError, ValueError) as error:
            raise ConfigurationError(f"{name} has an invalid value: {value!r}") from error
        _set_nested(result, keys, converted)

    def set_path(name: str, *keys: str) -> None:
        set_if(
            name,
            *keys,
            convert=lambda value: str(
                _expand_path(value, base=Path.cwd(), home=home)
            ),
        )

    set_if("LAB_HOST", "server", "host")
    set_if("LAB_PORT", "server", "port", convert=int)
    set_if("LAB_MCP_PORT", "server", "mcp_port", convert=int)
    set_path("LAB_PROJECT_ROOT", "knowledge", "project_root")
    set_path("LAB_WIKI_ROOT", "knowledge", "wiki_root")
    set_path("LAB_GSWIKI_DB", "knowledge", "gswiki_database")
    set_if(
        "LAB_GSWIKI_MAX_AGE_HOURS",
        "knowledge",
        "mirror_max_age_hours",
        convert=float,
    )
    if environment.get("LAB_INVENTORY_DB", "").strip():
        set_path("LAB_INVENTORY_DB", "storage", "inventory_database")
    elif environment.get("LAB_LICH_DATA_DIR", "").strip():
        # Preserve the legacy selection order: an explicit Lich data directory
        # selects its per-game ledger over a lower-precedence configured DB.
        _set_nested(result, ("storage", "inventory_database"), None)
    set_path("LAB_LICH_DATA_DIR", "storage", "lich_data_directory")
    set_if("LAB_GAME", "storage", "game")
    set_path("LAB_ACTION_TOKEN_FILE", "storage", "action_token_file")
    set_path("LAB_AUDIT_LOG", "storage", "audit_log")
    set_path("LAB_CONTROLLER_MANIFEST", "storage", "controller_manifest")
    set_if(
        "LAB_DECISIONS_ENABLED",
        "decisions",
        "enabled",
        convert=lambda value: _environment_bool("LAB_DECISIONS_ENABLED", value),
    )
    set_if("LAB_JEV_BASE_URL", "decisions", "base_url")
    set_if("LAB_DECISION_ENDPOINT_PATH", "decisions", "endpoint_path")
    set_if("LAB_JEV_MODEL", "decisions", "model")
    if "LAB_JEV_CREDENTIAL_ENV" in environment:
        credential_env = environment["LAB_JEV_CREDENTIAL_ENV"].strip()
        _set_nested(
            result,
            ("decisions", "credential_env"),
            credential_env or None,
        )
    set_if("LAB_JEV_TIMEOUT", "decisions", "timeout_seconds", convert=float)
    set_if(
        "LAB_DECISION_PLAYER_INTERVAL",
        "decisions",
        "player_interval_seconds",
        convert=float,
    )
    set_if("LAB_DECISION_TTL", "decisions", "decision_ttl_seconds", convert=float)
    set_path("LAB_DECISION_AUDIT_LOG", "decisions", "audit_log")

    selected = environment.get("LAB_PROFILE", "").strip() or str(
        current.get("selected_profile", "default")
    )
    if environment.get("LAB_PROFILE", "").strip():
        result["selected_profile"] = selected

    current_profiles = current.get("profiles", {})
    current_profile = (
        current_profiles.get(selected, {})
        if isinstance(current_profiles, Mapping)
        else {}
    )
    backend = environment.get("LAB_BACKEND", "").strip().casefold()
    if backend:
        if backend not in {"codex", "openai"}:
            raise ConfigurationError("LAB_BACKEND must be 'codex' or 'openai'")
        _set_nested(result, ("profiles", selected, "provider"), backend)
    else:
        backend = str(current_profile.get("provider", "codex")).casefold()

    if backend == "codex":
        set_if("LAB_CODEX_MODEL", "profiles", selected, "model")
    elif backend == "openai":
        set_if("LAB_MODEL", "profiles", selected, "model")
    set_if("LAB_REASONING_EFFORT", "profiles", selected, "reasoning_effort")
    set_path("LAB_INSTRUCTIONS_FILE", "profiles", selected, "instructions_file")
    set_if(
        "LAB_WEB_SEARCH",
        "profiles",
        selected,
        "web_search",
        convert=lambda value: _environment_bool("LAB_WEB_SEARCH", value),
    )
    set_if(
        "LAB_CODEX_BIN",
        "providers",
        "codex",
        "command",
        convert=lambda value: (
            str(_expand_path(value, base=Path.cwd(), home=home))
            if value.startswith("~") or "/" in value
            else value
        ),
    )
    return result


def _build_settings(
    raw: Mapping[str, Any], *, path: Path, environment: Mapping[str, str]
) -> Settings:
    _validate_schema_shape(raw, source="resolved settings")
    version = _integer(raw["schema_version"], "schema_version")
    if version != SCHEMA_VERSION:
        raise ConfigurationError(
            f"unsupported schema_version {version}; expected {SCHEMA_VERSION}"
        )
    base = path.parent
    home = _home_directory(environment)

    server_raw = _table(raw["server"], "server")
    host = _nonblank(server_raw["host"], "server.host")
    _validate_loopback(host)
    port = _integer(server_raw["port"], "server.port")
    if not 1 <= port <= 65_535:
        raise ConfigurationError("server.port must be between 1 and 65535")
    mcp_port = _integer(server_raw["mcp_port"], "server.mcp_port")
    if not 1 <= mcp_port <= 65_535:
        raise ConfigurationError("server.mcp_port must be between 1 and 65535")
    if mcp_port == port:
        raise ConfigurationError("server.mcp_port must differ from server.port")

    knowledge_raw = _table(raw["knowledge"], "knowledge")
    project_root = _required_path(
        knowledge_raw["project_root"], "knowledge.project_root", base, home
    )
    wiki_value = knowledge_raw.get("wiki_root")
    wiki_root = (
        project_root / "wiki"
        if wiki_value is None
        else _required_path(wiki_value, "knowledge.wiki_root", base, home)
    )
    database_value = knowledge_raw.get("gswiki_database")
    gswiki_database = (
        project_root / ".lab-cache" / "gswiki.sqlite3"
        if database_value is None
        else _required_path(
            database_value, "knowledge.gswiki_database", base, home
        )
    )
    semantic_model_directory = _optional_path(
        knowledge_raw.get("semantic_model_directory"),
        "knowledge.semantic_model_directory", base, home,
    )
    if semantic_model_directory is not None and "\x00" in str(semantic_model_directory):
        raise ConfigurationError("knowledge.semantic_model_directory must not contain a null byte")
    max_age = _number(
        knowledge_raw["mirror_max_age_hours"],
        "knowledge.mirror_max_age_hours",
    )
    if not 0 <= max_age <= 87_600:
        raise ConfigurationError(
            "knowledge.mirror_max_age_hours must be between 0 and 87600"
        )
    fallback = _enum_value(
        OnlineFallbackPolicy,
        knowledge_raw["online_fallback"],
        "knowledge.online_fallback",
    )
    general_web_provider = _enum_value(
        GeneralWebProvider,
        knowledge_raw["general_web_provider"],
        "knowledge.general_web_provider",
    )
    general_web_credential_env = _optional_nonblank(
        knowledge_raw.get("general_web_credential_env"),
        "knowledge.general_web_credential_env",
    )
    if general_web_credential_env is not None and not _ENVIRONMENT_NAME.fullmatch(
        general_web_credential_env
    ):
        raise ConfigurationError(
            "knowledge.general_web_credential_env must be an environment variable name"
        )
    if (
        general_web_provider is GeneralWebProvider.BRAVE
        and general_web_credential_env is None
    ):
        raise ConfigurationError(
            "knowledge.general_web_credential_env is required for the brave provider"
        )

    storage_raw = _table(raw["storage"], "storage")
    state_directory = _required_path(
        storage_raw["state_directory"], "storage.state_directory", base, home
    )
    inventory_database = _optional_path(
        storage_raw.get("inventory_database"),
        "storage.inventory_database",
        base,
        home,
    )
    lich_data_directory = _optional_path(
        storage_raw.get("lich_data_directory"),
        "storage.lich_data_directory",
        base,
        home,
    )
    game = _nonblank(storage_raw["game"], "storage.game")
    action_token_value = storage_raw.get("action_token_file")
    action_token_file = (
        state_directory / "action-token"
        if action_token_value is None
        else _required_path(
            action_token_value, "storage.action_token_file", base, home
        )
    )
    audit_value = storage_raw.get("audit_log")
    audit_log = (
        state_directory / "actions.jsonl"
        if audit_value is None
        else _required_path(audit_value, "storage.audit_log", base, home)
    )
    controller_value = storage_raw.get("controller_manifest")
    controller_manifest = (
        project_root / "lich" / "lab-controllers.json"
        if controller_value is None
        else _required_path(
            controller_value, "storage.controller_manifest", base, home
        )
    )

    decisions_raw = _table(raw["decisions"], "decisions")
    decision_enabled = _boolean(
        decisions_raw.get("enabled"), "decisions.enabled"
    )
    decision_kind = _enum_value(
        DecisionProviderKind, decisions_raw.get("kind"), "decisions.kind"
    )
    decision_base_url = _nonblank(
        decisions_raw.get("base_url"), "decisions.base_url"
    ).rstrip("/")
    _validate_url(decision_base_url, "decisions.base_url")
    parsed_decision_url = urlsplit(decision_base_url)
    secure_remote = parsed_decision_url.scheme == "https"
    local_http = (
        parsed_decision_url.scheme == "http"
        and _is_loopback_host(parsed_decision_url.hostname or "")
    )
    if not (secure_remote or local_http):
        raise ConfigurationError(
            "decisions.base_url must use HTTPS unless its HTTP host is loopback"
        )
    decision_endpoint_path = _decision_endpoint_path(
        decisions_raw.get("endpoint_path"), "decisions.endpoint_path"
    )
    decision_credential_value = decisions_raw.get("credential_env")
    if decision_credential_value is None or decision_credential_value == "":
        decision_credential_env = None
    else:
        decision_credential_env = _nonblank(
            decision_credential_value, "decisions.credential_env"
        )
    if (
        decision_credential_env is not None
        and not _ENVIRONMENT_NAME.fullmatch(decision_credential_env)
    ):
        raise ConfigurationError(
            "decisions.credential_env must be an environment variable name"
        )
    decision_model = _nonblank(decisions_raw.get("model"), "decisions.model")
    decision_timeout = _number(
        decisions_raw.get("timeout_seconds"), "decisions.timeout_seconds"
    )
    if not 0.1 <= decision_timeout <= 10:
        raise ConfigurationError(
            "decisions.timeout_seconds must be between 0.1 and 10"
        )
    decision_player_interval = _number(
        decisions_raw.get("player_interval_seconds"),
        "decisions.player_interval_seconds",
    )
    if not 0.5 <= decision_player_interval <= 60:
        raise ConfigurationError(
            "decisions.player_interval_seconds must be between 0.5 and 60"
        )
    decision_ttl = _number(
        decisions_raw.get("decision_ttl_seconds"),
        "decisions.decision_ttl_seconds",
    )
    if not 0.1 <= decision_ttl <= 5:
        raise ConfigurationError(
            "decisions.decision_ttl_seconds must be between 0.1 and 5"
        )
    decision_audit_value = decisions_raw.get("audit_log")
    decision_audit_log = (
        state_directory / "decisions-shadow.jsonl"
        if decision_audit_value is None
        else _required_path(
            decision_audit_value, "decisions.audit_log", base, home
        )
    )

    providers_raw = _table(raw["providers"], "providers")
    if not providers_raw:
        raise ConfigurationError("providers must contain at least one provider")
    providers: dict[str, ProviderSettings] = {}
    for name, value in providers_raw.items():
        _validate_name(name, f"providers.{name}")
        provider_raw = _table(value, f"providers.{name}")
        kind = _enum_value(
            ProviderKind, provider_raw.get("kind"), f"providers.{name}.kind"
        )
        command = _optional_nonblank(
            provider_raw.get("command"), f"providers.{name}.command"
        )
        base_url = _optional_nonblank(
            provider_raw.get("base_url"), f"providers.{name}.base_url"
        )
        credential_env = _optional_nonblank(
            provider_raw.get("credential_env"),
            f"providers.{name}.credential_env",
        )
        if credential_env is not None and not _ENVIRONMENT_NAME.fullmatch(
            credential_env
        ):
            raise ConfigurationError(
                f"providers.{name}.credential_env must be an environment variable name"
            )
        if kind is ProviderKind.CODEX:
            if command is None:
                raise ConfigurationError(
                    f"providers.{name}.command is required for a codex provider"
                )
            if base_url is not None or credential_env is not None:
                raise ConfigurationError(
                    f"providers.{name} is codex and cannot set base_url or credential_env"
                )
            if command.startswith("~") or "/" in command:
                command = str(_expand_path(command, base=base, home=home))
        else:
            if command is not None:
                raise ConfigurationError(
                    f"providers.{name} is HTTP-based and cannot set command"
                )
            if base_url is None:
                raise ConfigurationError(
                    f"providers.{name}.base_url is required for an HTTP provider"
                )
            _validate_url(base_url, f"providers.{name}.base_url")
            if kind is ProviderKind.OPENAI and credential_env is None:
                raise ConfigurationError(
                    f"providers.{name}.credential_env is required for an OpenAI provider"
                )
        providers[name] = ProviderSettings(
            kind=kind,
            command=command,
            base_url=base_url.rstrip("/") if base_url is not None else None,
            credential_env=credential_env,
        )

    profiles_raw = _table(raw["profiles"], "profiles")
    if not profiles_raw:
        raise ConfigurationError("profiles must contain at least one agent profile")
    profiles: dict[str, AgentProfile] = {}
    for name, value in profiles_raw.items():
        _validate_name(name, f"profiles.{name}")
        profile_raw = _table(value, f"profiles.{name}")
        provider = _nonblank(
            profile_raw.get("provider"), f"profiles.{name}.provider"
        )
        if provider not in providers:
            raise ConfigurationError(
                f"profiles.{name}.provider references unknown provider {provider!r}"
            )
        model = _optional_nonblank(
            profile_raw.get("model"), f"profiles.{name}.model"
        )
        effort = _optional_nonblank(
            profile_raw.get("reasoning_effort"),
            f"profiles.{name}.reasoning_effort",
        )
        if effort is not None:
            effort = effort.casefold()
            if effort not in _REASONING_EFFORTS:
                allowed = ", ".join(sorted(_REASONING_EFFORTS))
                raise ConfigurationError(
                    f"profiles.{name}.reasoning_effort must be one of: {allowed}"
                )
        timeout = _number(
            profile_raw.get("timeout_seconds"),
            f"profiles.{name}.timeout_seconds",
        )
        if not 1 <= timeout <= 600:
            raise ConfigurationError(
                f"profiles.{name}.timeout_seconds must be between 1 and 600"
            )
        instructions_file = _optional_path(
            profile_raw.get("instructions_file"),
            f"profiles.{name}.instructions_file",
            base,
            home,
        )
        web_search = _boolean(
            profile_raw.get("web_search"), f"profiles.{name}.web_search"
        )
        evidence_result = _integer(
            profile_raw.get("evidence_result_chars", DEFAULT_EVIDENCE_RESULT_CHARS),
            f"profiles.{name}.evidence_result_chars",
        )
        evidence_total = _integer(
            profile_raw.get("evidence_total_chars", DEFAULT_EVIDENCE_TOTAL_CHARS),
            f"profiles.{name}.evidence_total_chars",
        )
        if not 3_000 <= evidence_result <= 100_000:
            raise ConfigurationError(
                f"profiles.{name}.evidence_result_chars must be between 3000 and 100000"
            )
        # Reserve omission notices for all eight evidence requests, even when a
        # single result uses the full per-result allowance.
        if not evidence_result + 2_400 <= evidence_total <= 300_000:
            raise ConfigurationError(
                f"profiles.{name}.evidence_total_chars must be at least "
                "evidence_result_chars + 2400 and at most 300000"
            )
        if providers[provider].kind is ProviderKind.OPENAI and model is None:
            model = DEFAULT_OPENAI_MODEL
        if (
            providers[provider].kind is ProviderKind.OPENAI_COMPATIBLE
            and model is None
        ):
            raise ConfigurationError(
                f"profiles.{name}.model is required for HTTP provider {provider!r}"
            )
        profiles[name] = AgentProfile(
            provider=provider,
            model=model,
            reasoning_effort=effort,
            timeout_seconds=timeout,
            instructions_file=instructions_file,
            web_search=web_search,
            evidence_result_chars=evidence_result,
            evidence_total_chars=evidence_total,
        )

    selected_profile = _nonblank(raw["selected_profile"], "selected_profile")
    if selected_profile not in profiles:
        raise ConfigurationError(
            f"selected_profile references unknown profile {selected_profile!r}"
        )
    return Settings(
        path=path,
        schema_version=version,
        selected_profile_name=selected_profile,
        server=ServerSettings(host=host, port=port, mcp_port=mcp_port),
        knowledge=KnowledgeSettings(
            project_root=project_root,
            wiki_root=wiki_root,
            gswiki_database=gswiki_database,
            mirror_max_age_hours=max_age,
            online_fallback=fallback,
            general_web_provider=general_web_provider,
            general_web_credential_env=general_web_credential_env,
            semantic_model_directory=semantic_model_directory,
        ),
        storage=StorageSettings(
            state_directory=state_directory,
            inventory_database=inventory_database,
            lich_data_directory=lich_data_directory,
            game=game,
            action_token_file=action_token_file,
            audit_log=audit_log,
            controller_manifest=controller_manifest,
        ),
        decisions=DecisionSettings(
            enabled=decision_enabled,
            kind=decision_kind,
            base_url=decision_base_url,
            endpoint_path=decision_endpoint_path,
            credential_env=decision_credential_env,
            model=decision_model,
            timeout_seconds=decision_timeout,
            player_interval_seconds=decision_player_interval,
            decision_ttl_seconds=decision_ttl,
            audit_log=decision_audit_log,
        ),
        providers=MappingProxyType(providers),
        profiles=MappingProxyType(profiles),
    )


def _validate_schema_shape(
    value: Mapping[str, Any], *, source: str, require_version: bool = True
) -> None:
    _unknown_keys(value, _TOP_LEVEL_KEYS, source)
    if require_version and "schema_version" not in value:
        raise ConfigurationError(f"{source} is missing required key schema_version")
    for key, allowed in (
        ("server", _SERVER_KEYS),
        ("knowledge", _KNOWLEDGE_KEYS),
        ("storage", _STORAGE_KEYS),
        ("decisions", _DECISION_KEYS),
    ):
        if key in value:
            _unknown_keys(_table(value[key], key), allowed, key)
    for key, allowed in (("providers", _PROVIDER_KEYS), ("profiles", _PROFILE_KEYS)):
        if key not in value:
            continue
        table = _table(value[key], key)
        for name, nested in table.items():
            _validate_name(name, f"{key}.{name}")
            nested_table = _table(nested, f"{key}.{name}")
            if key == "providers" and "api_key" in nested_table:
                raise ConfigurationError(
                    f"{key}.{name}.api_key must not be stored; use credential_env"
                )
            _unknown_keys(nested_table, allowed, f"{key}.{name}")


def _unknown_keys(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(
            f"unknown setting {label}.{unknown[0]}" if label else f"unknown setting {unknown[0]}"
        )


def _copy_mapping(value: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ConfigurationError(f"{label} keys must be strings")
        result[key] = (
            _copy_mapping(item, label=f"{label}.{key}")
            if isinstance(item, Mapping)
            else item
        )
    return result


def _merge(target: dict[str, Any], update: Mapping[str, Any]) -> None:
    for key, value in update.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _merge(current, value)
        else:
            target[key] = (
                _copy_mapping(value, label=key)
                if isinstance(value, Mapping)
                else value
            )


def _set_nested(target: dict[str, Any], keys: tuple[str, ...], value: Any) -> None:
    current = target
    for key in keys[:-1]:
        current = current.setdefault(key, {})
    current[keys[-1]] = value


def _table(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{label} must be a table")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{label} must be an integer")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{label} must be a number")
    return float(value)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{label} must be true or false")
    return value


def _nonblank(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label} must be a nonblank string")
    return value.strip()


def _optional_nonblank(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _nonblank(value, label)


def _validate_name(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ConfigurationError(
            f"{label} must use letters, numbers, underscores, or hyphens and start with a letter"
        )


def _required_path(value: Any, label: str, base: Path, home: Path) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ConfigurationError(f"{label} must be a nonblank path")
    return _expand_path(value, base=base, home=home)


def _optional_path(
    value: Any, label: str, base: Path, home: Path
) -> Path | None:
    if value is None:
        return None
    return _required_path(value, label, base, home)


def _expand_path(
    value: str | os.PathLike[str], *, base: Path, home: Path
) -> Path:
    text = os.fspath(value)
    if text == "~":
        result = home
    elif text.startswith("~/"):
        result = home / text[2:]
    elif text.startswith("~"):
        raise ConfigurationError(
            f"unsupported home-relative path {text!r}; use ~ or ~/path"
        )
    else:
        result = Path(text)
    if not result.is_absolute():
        result = base / result
    return Path(os.path.abspath(result))


def _home_directory(environment: Mapping[str, str]) -> Path:
    configured = environment.get("HOME", "").strip()
    if configured:
        return Path(os.path.abspath(configured))
    return Path.home()


def _validate_loopback(host: str) -> None:
    if _is_loopback_host(host):
        return
    raise ConfigurationError(
        "server.host must be localhost or a loopback IP address"
    )


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback


def _validate_url(value: str, label: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(f"{label} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(f"{label} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError(f"{label} must not contain a query or fragment")


def _decision_endpoint_path(value: Any, label: str) -> str:
    path = _nonblank(value, label)
    if len(path) > _MAX_DECISION_ENDPOINT_PATH_CHARS:
        raise ConfigurationError(
            f"{label} must be at most {_MAX_DECISION_ENDPOINT_PATH_CHARS} characters"
        )
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
    ):
        raise ConfigurationError(f"{label} must be an absolute URL path")
    if parsed.query or parsed.fragment or parsed.path != path:
        raise ConfigurationError(f"{label} must not contain a query or fragment")
    if "\\" in path:
        raise ConfigurationError(f"{label} must not contain a backslash")
    if any(ord(character) < 0x21 or ord(character) == 0x7F for character in path):
        raise ConfigurationError(f"{label} must not contain whitespace or controls")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise ConfigurationError(f"{label} must not contain dot segments")
    return path


def _enum_value(enum: type[StrEnum], value: Any, label: str) -> Any:
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} must be a string")
    try:
        return enum(value.strip().casefold())
    except ValueError as error:
        allowed = ", ".join(item.value for item in enum)
        raise ConfigurationError(f"{label} must be one of: {allowed}") from error


def _environment_bool(name: str, value: str) -> bool:
    folded = value.casefold()
    if folded in {"1", "true", "yes", "on"}:
        return True
    if folded in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _path_text(value: Path | None) -> str | None:
    return str(value) if value is not None else None


def _toml_document(value: Mapping[str, Any]) -> str:
    lines = [
        f"schema_version = {value['schema_version']}",
        f"selected_profile = {_toml_string(value['selected_profile'])}",
    ]
    for table_name in ("server", "knowledge", "storage", "decisions"):
        lines.extend(("", f"[{table_name}]"))
        _append_toml_values(lines, value[table_name])
    for table_name in ("providers", "profiles"):
        for name in sorted(value[table_name]):
            lines.extend(("", f"[{table_name}.{name}]"))
            _append_toml_values(lines, value[table_name][name])
    return "\n".join(lines) + "\n"


def _append_toml_values(lines: list[str], values: Mapping[str, Any]) -> None:
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            rendered = repr(value)
        elif isinstance(value, str):
            rendered = _toml_string(value)
        else:
            raise ConfigurationError(f"cannot serialize setting {key}")
        lines.append(f"{key} = {rendered}")


def _toml_string(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("cannot serialize a non-string TOML value")
    return json.dumps(value, ensure_ascii=False)


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
