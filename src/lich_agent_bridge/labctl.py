"""Read the loopback-only LAB session hub from a shell or outside agent."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .errors import ConfigurationError, ValidationError
from .controller_manifest import CONTROLLER_CONTROLS
from .gswiki import DEFAULT_NAMESPACES, sync
from .local_connection import read_action_token
from .question_tests import QuestionSession, load_corpus, run_corpus, validate_question
from .settings import GeneralWebProvider, ProviderKind, Settings
from .timings import timing_report
from .world_state import MAX_WATCH_TIMEOUT_SECONDS


MAX_DOCTOR_RESPONSE_BYTES = 1_048_576


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--config",
        type=Path,
        help="use this settings file instead of LAB_CONFIG or the XDG default",
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="create or revise the LAB settings file")

    config = commands.add_parser("config", help="inspect LAB configuration")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("path", help="show the active settings file path")
    config_commands.add_parser(
        "show", help="show resolved settings without credential values"
    )

    commands.add_parser(
        "doctor", help="check local configuration and service readiness"
    )
    commands.add_parser("status", help="show sidecar health")
    commands.add_parser("timings", help="summarize local latency measurements")

    wiki = commands.add_parser(
        "wiki", help="inspect or explicitly refresh the local GSWiki mirror"
    )
    wiki_commands = wiki.add_subparsers(dest="wiki_command", required=True)
    wiki_commands.add_parser("status", help="show mirror health, size, and freshness")
    wiki_commands.add_parser(
        "index", help="atomically build the local passage index without downloading wiki pages"
    )
    refresh = wiki_commands.add_parser(
        "refresh", help="atomically refresh the local GSWiki mirror"
    )
    refresh.add_argument("--namespace", dest="namespaces", type=int, action="append")
    refresh.add_argument("--delay", type=float, default=0.05)

    state = commands.add_parser("state", help="show the current character snapshot")
    state.add_argument("character")

    ask = commands.add_parser(
        "ask", help="ask one question using current state (read-only by default)"
    )
    ask.add_argument("character")
    ask.add_argument("question")
    ask.add_argument(
        "--allow-recon", action="store_true",
        help="allow fixed INFO/SKILLS recon through existing action and approval gates",
    )

    questions = commands.add_parser(
        "questions", help="record a private sequential corpus (read-only by default)"
    )
    questions.add_argument("character")
    questions.add_argument("corpus", type=Path)
    questions.add_argument(
        "--output", type=Path, required=True,
        help="new private result JSON file (never overwritten)",
    )
    questions.add_argument(
        "--allow-recon", action="store_true",
        help="allow fixed INFO/SKILLS recon through existing action and approval gates",
    )

    tests = commands.add_parser("tests", help="prepare explicitly trusted script suites offline")
    test_commands = tests.add_subparsers(dest="test_command", required=True)
    prepare = test_commands.add_parser("prepare", help="validate files and print an uninstalled controller entry")
    prepare.add_argument("manifest", type=Path)
    prepare.add_argument("--scripts-dir", type=Path, required=True)
    prepare.add_argument("--character", required=True)
    prepare.add_argument("--room-id", required=True)

    watch = commands.add_parser("watch", help="stream meaningful character events")
    watch.add_argument("character")
    watch.add_argument("--cursor", type=int, default=0)
    watch.add_argument(
        "--timeout", type=float, default=MAX_WATCH_TIMEOUT_SECONDS
    )
    watch.add_argument(
        "--once",
        action="store_true",
        help="return after one watch response, including a timeout",
    )

    inventory = commands.add_parser("inventory", help="search durable inventory facts")
    inventory.add_argument("character")
    inventory.add_argument("query")

    combat_report = commands.add_parser("combat-report", help="read retained LAB trial combat evidence")
    combat_report.add_argument("character")
    combat_report.add_argument("--operation-id")

    sources = commands.add_parser(
        "sources", help="show references supplied to a character's most recent answer"
    )
    sources.add_argument("character")

    perform = commands.add_parser("perform", help="start one bounded capability")
    perform.add_argument("character")
    perform.add_argument("capability")
    perform.add_argument("--item-id")
    perform.add_argument("--method", action="append", default=[])
    perform.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="KEY=JSON",
        help="capability-specific JSON argument; may be repeated",
    )
    perform.add_argument("--wait", action="store_true")
    perform.add_argument("--timeout", type=float, default=30.0,
                         help="Watch long-poll seconds (0–30); does not extend the operation.")
    perform.add_argument("--operation-timeout", type=float, default=30.0,
                         help="Total operation seconds (default 30; refuge outings up to 300, including recovery).")
    perform.add_argument("--expected-generation", help="bind admission to the observed session generation")

    stop = commands.add_parser("stop", help="interrupt a character's active operation")
    stop.add_argument("character")
    stop.add_argument("--operation-id", help="stop this exact operation, not a successor")
    stop.add_argument("--expected-generation", help="required with --operation-id")

    control = commands.add_parser("control", help="request a registered control on an exact active operation")
    control.add_argument("character")
    control.add_argument("control", choices=sorted(CONTROLLER_CONTROLS))
    control.add_argument("--operation-id", required=True, help="control this exact operation, not a successor")
    control.add_argument("--expected-generation", required=True, help="bind the control to the operation's session")
    return result


def _base_url(settings: Settings | None = None) -> str:
    selected = settings or Settings.load()
    host = selected.server.host
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}:{selected.server.port}"


def _token(settings: Settings | None = None) -> str:
    selected = settings or Settings.load()
    try:
        token = read_action_token(selected.storage.action_token_file)
    except ConfigurationError as error:
        raise SystemExit(f"cannot read LAB token: {error}") from error
    return token


def _request(
    path: str,
    *,
    settings: Settings | None = None,
    token: str | None = None,
    timeout: float = 3,
) -> Any:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(_base_url(settings) + path, method="GET", headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except HTTPError as error:
        try:
            body = json.loads(error.read())
            detail = body.get("detail", body.get("error", error.reason)) if isinstance(body, dict) else error.reason
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            detail = error.reason
        raise SystemExit(f"LAB request failed ({error.code}): {detail}") from error
    except URLError as error:
        raise SystemExit(f"sidecar unavailable: {error.reason}") from error
    except (json.JSONDecodeError, UnicodeError, RecursionError) as error:
        raise SystemExit("sidecar returned invalid JSON") from error


def _post(
    path: str,
    payload: dict[str, Any],
    *,
    settings: Settings | None = None,
    token: str,
    timeout: float = 3,
) -> Any:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    request = Request(
        _base_url(settings) + path,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except HTTPError as error:
        try:
            body = json.loads(error.read())
            detail = body.get("detail", body.get("error", error.reason)) if isinstance(body, dict) else error.reason
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            detail = error.reason
        raise SystemExit(f"LAB request failed ({error.code}): {detail}") from error
    except URLError as error:
        raise SystemExit(f"sidecar unavailable: {error.reason}") from error
    except (json.JSONDecodeError, UnicodeError, RecursionError) as error:
        raise SystemExit("sidecar returned invalid JSON") from error


def _dump(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _prompt(prompt: str, current: Any) -> str:
    rendered = "" if current is None else str(current)
    answer = input(f"{prompt} [{rendered}]: ").strip()
    return answer or rendered


def _prompt_optional(prompt: str, current: Any) -> str | None:
    rendered = "unset" if current is None else str(current)
    answer = input(f"{prompt} [{rendered}; '-' clears]: ").strip()
    if not answer:
        return current
    if answer == "-":
        return None
    return answer


def _prompt_int(prompt: str, current: int) -> int:
    try:
        return int(_prompt(prompt, current))
    except ValueError as error:
        raise SystemExit(f"{prompt} must be an integer") from error


def _prompt_float(prompt: str, current: float) -> float:
    try:
        return float(_prompt(prompt, current))
    except ValueError as error:
        raise SystemExit(f"{prompt} must be a number") from error


def _yes(prompt: str, *, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{suffix}]: ").strip().casefold()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _setup(path: Path) -> None:
    exists = path.exists()
    if exists:
        try:
            current = Settings.load(path=path)
            detail = "valid"
        except ConfigurationError as error:
            current = Settings.load(path=path.with_name(f".{path.name}.defaults"))
            detail = f"invalid: {error}"
        print(f"Existing configuration: {path} ({detail})")
        question = (
            "Replace this configuration and keep its resolved values as defaults"
            if detail == "valid"
            else "Replace this invalid configuration using detected defaults"
        )
        if not _yes(question):
            print(f"Preserved {path}")
            return
    else:
        current = Settings.load(path=path)
        print(f"New configuration: {path}")

    values = current.to_mapping()
    available = "server, knowledge, agent, storage"
    raw_sections = input(
        f"Sections to edit ({available}; Enter accepts detected defaults) []: "
    ).strip()
    sections = {
        section.strip().casefold()
        for section in raw_sections.split(",")
        if section.strip()
    }
    if "all" in sections:
        sections = {"server", "knowledge", "agent", "storage"}
    unknown = sections - {"server", "knowledge", "agent", "storage"}
    if unknown:
        raise SystemExit(
            "unknown setup section(s): " + ", ".join(sorted(unknown))
        )

    if "server" in sections:
        server = values["server"]
        server["host"] = _prompt("SessionHub host", server["host"])
        server["port"] = _prompt_int("SessionHub port", server["port"])
        server["mcp_port"] = _prompt_int("MCP adapter port", server["mcp_port"])
    if "knowledge" in sections:
        knowledge = values["knowledge"]
        knowledge["project_root"] = _prompt(
            "LAB project root", knowledge["project_root"]
        )
        knowledge["wiki_root"] = _prompt(
            "Curated wiki root", knowledge["wiki_root"]
        )
        knowledge["gswiki_database"] = _prompt(
            "Local GSWiki database", knowledge["gswiki_database"]
        )
        knowledge["semantic_model_directory"] = _prompt_optional(
            "Optional local semantic model directory (unset keeps lexical retrieval)",
            knowledge["semantic_model_directory"],
        )
        knowledge["mirror_max_age_hours"] = _prompt_float(
            "Mirror freshness threshold in hours",
            knowledge["mirror_max_age_hours"],
        )
        knowledge["online_fallback"] = _prompt(
            "Online GSWiki fallback policy",
            knowledge["online_fallback"],
        )
        knowledge["general_web_provider"] = _prompt(
            "Optional general web provider (disabled or brave)",
            knowledge["general_web_provider"],
        )
        knowledge["general_web_credential_env"] = _prompt_optional(
            "General web credential environment variable (optional)",
            knowledge["general_web_credential_env"],
        )
    if "agent" in sections:
        profile_name = _prompt("Selected agent profile", values["selected_profile"])
        if profile_name not in values["profiles"]:
            values["profiles"][profile_name] = dict(
                values["profiles"][values["selected_profile"]]
            )
        values["selected_profile"] = profile_name
        profile = values["profiles"][profile_name]
        profile["provider"] = _prompt("Agent provider", profile["provider"])
        profile["model"] = _prompt_optional(
            "Model (optional)", profile["model"]
        )
        profile["reasoning_effort"] = _prompt_optional(
            "Reasoning effort", profile["reasoning_effort"]
        )
        profile["instructions_file"] = _prompt_optional(
            "Custom instructions file (optional)", profile["instructions_file"]
        )
        profile["web_search"] = _yes(
            "Allow optional general web fallback",
            default=bool(profile["web_search"]),
        )
        provider_name = profile["provider"]
        if provider_name in values["providers"]:
            provider = values["providers"][provider_name]
            if provider["kind"] == ProviderKind.CODEX.value:
                provider["command"] = _prompt(
                    "Codex CLI command", provider["command"]
                )
            else:
                provider["base_url"] = _prompt(
                    "Provider base URL", provider["base_url"]
                )
                provider["credential_env"] = _prompt_optional(
                    "Credential environment variable name (optional)",
                    provider["credential_env"],
                )
    if "storage" in sections:
        storage = values["storage"]
        for field, label in (
            ("inventory_database", "Inventory database (optional)"),
            ("lich_data_directory", "Lich data directory (optional)"),
            ("controller_manifest", "Controller manifest (optional)"),
        ):
            storage[field] = _prompt_optional(label, storage[field])
        for field, label in (
            ("game", "Lich game data directory name"),
            ("action_token_file", "Action token path"),
            ("audit_log", "Audit log path"),
        ):
            storage[field] = _prompt(label, storage[field])

    try:
        # For an invalid destination, ``current`` came from the clean defaults
        # path selected above.  Validate those answers without parsing the
        # known-invalid destination a second time.
        resolved = Settings.load(path=current.path, environment={}, overrides=values)
        written = resolved.write(path=path, replace=exists)
    except (ConfigurationError, ValueError) as error:
        raise SystemExit(f"configuration was not written: {error}") from error
    print(f"Wrote {written} atomically")
    for check in _setup_checks(resolved):
        print(f"{check['status'].upper()}: {check['name']}: {check['detail']}")
    print("Run `labctl doctor` to verify local resources and service readiness.")


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _path_check(name: str, value: str, *, kind: str) -> dict[str, str]:
    if not value:
        return _check(name, "warning", "not configured")
    path = Path(value)
    if kind == "directory" and path.is_dir():
        return _check(name, "ok", str(path))
    if kind == "file" and path.is_file():
        return _check(name, "ok", str(path))
    return _check(name, "warning", f"{kind} not found: {path}")


def _optional_path_check(name: str, value: str, *, kind: str) -> dict[str, str]:
    if not value:
        return _check(name, "ok", "not configured (optional)")
    return _path_check(name, value, kind=kind)


def _output_path_check(name: str, value: str) -> dict[str, str]:
    path = Path(value)
    if path.is_file():
        status = "ok" if os.access(path, os.W_OK) else "warning"
        detail = str(path) if status == "ok" else f"file is not writable: {path}"
        return _check(name, status, detail)
    parent = path.parent
    if parent.is_dir() and os.access(parent, os.W_OK):
        return _check(name, "ok", f"will be created under writable directory {parent}")
    return _check(name, "warning", f"parent directory is not writable: {parent}")


def _gswiki_check(path_value: str, max_age_hours: float) -> dict[str, str]:
    if not path_value:
        return _check("gswiki_database", "warning", "not configured")
    path = Path(path_value)
    if not path.is_file():
        return _check("gswiki_database", "warning", f"file not found: {path}")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        if not {"pages", "pages_fts", "metadata"}.issubset(tables):
            return _check(
                "gswiki_database", "error", "database is missing the GSWiki schema"
            )
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'last_sync'"
        ).fetchone()
    except sqlite3.Error as error:
        return _check("gswiki_database", "error", f"cannot read database: {error}")
    finally:
        if "connection" in locals():
            connection.close()
    if row is None:
        return _check("gswiki_database", "warning", "last_sync metadata is missing")
    try:
        last_sync = datetime.fromisoformat(str(row[0]))
        if last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - last_sync).total_seconds() / 3_600
    except ValueError:
        return _check("gswiki_database", "warning", "last_sync metadata is invalid")
    if age_hours > max_age_hours:
        return _check(
            "gswiki_database",
            "warning",
            f"mirror is {age_hours:.1f} hours old; run the explicit wiki refresh",
        )
    return _check(
        "gswiki_database", "ok", f"schema healthy; mirror age {age_hours:.1f} hours"
    )


def _wiki_status(settings: Settings) -> dict[str, Any]:
    path = settings.knowledge.gswiki_database
    summary: dict[str, Any] = {
        "path": str(path),
        "configured_freshness_hours": settings.knowledge.mirror_max_age_hours,
        "health": _gswiki_check(str(path), settings.knowledge.mirror_max_age_hours),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "last_sync": None,
        "passage_index": {"status": "missing"},
    }
    if not path.is_file():
        return summary
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'last_sync'"
        ).fetchone()
        summary["last_sync"] = None if row is None else row[0]
        from .passage_index import status as passage_status

        summary["passage_index"] = passage_status(connection)
    except sqlite3.Error:
        pass
    finally:
        if "connection" in locals():
            connection.close()
    return summary


def _wiki_index(settings: Settings) -> dict[str, Any]:
    from .passage_index import PassageIndexUnavailable, rebuild

    path = settings.knowledge.gswiki_database
    try:
        result = rebuild(path)
    except (OSError, ValueError, sqlite3.Error, PassageIndexUnavailable) as error:
        return {"status": "error", "database": str(path),
                "detail": f"passage indexing failed; previous mirror was retained: {error}"}
    return {"status": "ok", "database": str(path), "passage_index": result}


def _wiki_refresh(
    settings: Settings, *, namespaces: list[int] | None, delay: float
) -> dict[str, Any]:
    if delay < 0:
        raise SystemExit("--delay must be non-negative")
    try:
        result = sync(
            settings.knowledge.gswiki_database,
            namespaces=DEFAULT_NAMESPACES if namespaces is None else namespaces,
            delay=delay,
        )
    except Exception as error:
        return {
            "status": "error",
            "database": str(settings.knowledge.gswiki_database),
            "detail": f"refresh failed; previous mirror was retained: {error}",
        }
    return {
        "status": "ok",
        "database": str(result.database),
        "pages": result.pages,
        "namespaces": list(result.namespaces),
    }


def _inventory_check(path_value: str) -> dict[str, str]:
    if not path_value:
        return _check("inventory_database", "warning", "not configured")
    path = Path(path_value)
    if not path.is_file():
        return _check("inventory_database", "warning", f"file not found: {path}")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    except sqlite3.Error as error:
        return _check("inventory_database", "error", f"cannot read database: {error}")
    finally:
        if "connection" in locals():
            connection.close()
    required = {"characters", "dossiers", "facts", "session_locations"}
    missing = sorted(required - tables)
    if missing:
        return _check(
            "inventory_database",
            "error",
            "database is missing table(s): " + ", ".join(missing),
        )
    return _check("inventory_database", "ok", f"schema healthy: {path}")


def _general_web_check(settings: Settings, environment: dict[str, str]) -> dict[str, str]:
    if not settings.selected_profile.web_search:
        return _check("general_web", "ok", "disabled by selected profile")
    if settings.knowledge.general_web_provider is GeneralWebProvider.DISABLED:
        return _check("general_web", "warning", "selected profile enables web search but no provider is configured")
    credential = settings.knowledge.general_web_credential_env
    if credential is None or not environment.get(credential):
        return _check("general_web", "warning", f"environment variable {credential or 'for general web'} is not set")
    return _check("general_web", "ok", "brave general-web fallback configured")


def _semantic_check(settings: Settings) -> dict[str, str]:
    directory = settings.knowledge.semantic_model_directory
    if directory is None:
        return {**_check("semantic_model", "ok", "disabled; using lexical retrieval"),
                "semantic_status": "disabled"}
    from .semantic import inspect_model

    inspection = inspect_model(directory)
    status = inspection["status"]
    return {**_check("semantic_model", "ok" if status == "ready" else "warning",
                     inspection["detail"]), "semantic_status": status}


def _loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _service_check(
    host: str, port: int, *, name: str = "session_hub"
) -> dict[str, str]:
    if not _loopback_host(host):
        return _check(
            name, "error", f"refusing to probe non-loopback host {host!r}"
        )
    rendered_host = f"[{host}]" if ":" in host else host
    request = Request(
        f"http://{rendered_host}:{port}/health",
        method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=0.5) as response:
            body = json.loads(response.read())
    except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError) as error:
        return _check(
            name,
            "warning",
            f"not ready at {host}:{port}: {getattr(error, 'reason', error)}",
        )
    if not isinstance(body, dict):
        return _check(name, "warning", "health response was invalid")
    if body.get("status") != "ok":
        return _check(name, "warning", "health response was not ready")
    return _check(name, "ok", f"ready at {host}:{port}")


def _backend_check(
    values: dict[str, Any], environment: dict[str, str], *, probe: bool = False
) -> dict[str, str]:
    selected = values["selected_profile"]
    profile = values["profiles"][selected]
    provider_name = profile["provider"]
    providers = values["providers"]
    provider = providers[provider_name]
    if provider["kind"] == ProviderKind.CODEX.value:
        binary = provider["command"]
        resolved = shutil.which(binary) if binary else None
        if resolved:
            return _check("agent_backend", "ok", f"profile {selected}: {resolved}")
        return _check(
            "agent_backend", "warning", f"profile {selected}: Codex CLI not found"
        )
    key_name = provider.get("credential_env", "")
    if key_name and not environment.get(key_name):
        return _check(
            "agent_backend",
            "warning",
            f"profile {selected}: environment variable {key_name} is not set",
        )
    if probe:
        base_url = provider["base_url"]
        request = Request(
            f"{base_url}/models",
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=0.5) as response:
                payload = response.read(MAX_DOCTOR_RESPONSE_BYTES + 1)
        except HTTPError as error:
            if error.code in {401, 403}:
                return _check(
                    "agent_backend",
                    "warning",
                    f"profile {selected}: endpoint reachable; "
                    "authentication not probed",
                )
            return _check(
                "agent_backend",
                "warning",
                f"profile {selected}: endpoint not ready: HTTP {error.code}",
            )
        except (URLError, OSError) as error:
            return _check(
                "agent_backend",
                "warning",
                f"profile {selected}: endpoint not ready: "
                f"{getattr(error, 'reason', error)}",
            )
        if len(payload) > MAX_DOCTOR_RESPONSE_BYTES:
            return _check(
                "agent_backend",
                "warning",
                f"profile {selected}: endpoint response was too large",
            )
        try:
            body = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError):
            return _check(
                "agent_backend",
                "warning",
                f"profile {selected}: endpoint returned invalid JSON",
            )
        if not isinstance(body, dict):
            return _check(
                "agent_backend",
                "warning",
                f"profile {selected}: endpoint returned an invalid response",
            )
    return _check("agent_backend", "ok", f"profile {selected}: {provider_name}")


def _setup_checks(settings: Settings) -> list[dict[str, str]]:
    values = settings.to_mapping()
    knowledge = values["knowledge"]
    inventory = settings.storage.resolved_inventory_database
    return [
        _path_check("wiki_root", knowledge["wiki_root"], kind="directory"),
        _path_check(
            "gswiki_database", knowledge["gswiki_database"], kind="file"
        ),
        _optional_path_check(
            "inventory_database", str(inventory or ""), kind="file"
        ),
        _backend_check(values, dict(os.environ)),
    ]


def _doctor(path: Path) -> dict[str, Any]:
    try:
        settings = Settings.load(path=path)
    except ConfigurationError as error:
        return {
            "status": "error",
            "config_path": str(path),
            "checks": [_check("configuration", "error", str(error))],
        }
    values = settings.to_mapping()
    knowledge = values["knowledge"]
    storage = values["storage"]
    server = values["server"]
    profile = settings.selected_profile
    checks = [
        _check(
            "configuration",
            "ok" if path.is_file() else "warning",
            str(path) if path.is_file() else f"file not found; using defaults: {path}",
        ),
        _path_check("wiki_root", knowledge["wiki_root"], kind="directory"),
        _gswiki_check(
            knowledge["gswiki_database"], knowledge["mirror_max_age_hours"]
        ),
        _semantic_check(settings),
        _general_web_check(settings, dict(os.environ)),
        _inventory_check(str(settings.storage.resolved_inventory_database or "")),
        _optional_path_check(
            "lich_data_directory", storage["lich_data_directory"], kind="directory"
        ),
        _optional_path_check(
            "controller_manifest", storage["controller_manifest"], kind="file"
        ),
        _optional_path_check(
            "instructions_file", str(profile.instructions_file or ""), kind="file"
        ),
        _optional_path_check(
            "action_token_file", storage["action_token_file"], kind="file"
        ),
        _output_path_check("audit_log", storage["audit_log"]),
        _backend_check(values, dict(os.environ), probe=True),
        _service_check(server["host"], server["port"], name="session_hub"),
        _service_check("127.0.0.1", server["mcp_port"], name="mcp_adapter"),
    ]
    if any(check["status"] == "error" for check in checks):
        status = "error"
    elif any(check["status"] == "warning" for check in checks):
        status = "warning"
    else:
        status = "ok"
    return {"status": status, "config_path": str(path), "checks": checks}


def _perform_args(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if args.item_id is not None:
        result["item_id"] = args.item_id
    if args.method:
        result["methods"] = args.method
    for raw in args.arg:
        key, separator, value = raw.partition("=")
        if not separator or not key.strip():
            raise SystemExit("--arg must be KEY=JSON")
        try:
            result[key.strip()] = json.loads(value)
        except json.JSONDecodeError as error:
            raise SystemExit(f"invalid JSON for --arg {key}: {error.msg}") from error
    return result


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    if args.command == "tests":
        from .script_tests import prepare_script_suite

        try:
            registration = prepare_script_suite(
                args.manifest, args.scripts_dir, args.character, args.room_id
            )
        except (ConfigurationError, ValidationError, OSError) as error:
            raise SystemExit(f"cannot prepare script suite: {error}") from error
        _dump(registration)
        return
    active_config = Settings.path_for(args.config)
    if args.command == "setup":
        _setup(active_config)
        return
    if args.command == "config":
        if args.config_command == "path":
            print(active_config)
            return
        try:
            settings = Settings.load(path=active_config)
        except ConfigurationError as error:
            raise SystemExit(f"cannot load LAB configuration: {error}") from error
        _dump(settings.redacted())
        return
    if args.command == "doctor":
        _dump(_doctor(active_config))
        return
    try:
        settings = Settings.load(path=active_config)
    except ConfigurationError as error:
        raise SystemExit(f"cannot load LAB configuration: {error}") from error
    if args.command == "status":
        _dump(_request("/health", settings=settings))
        return
    if args.command == "timings":
        _dump(
            timing_report(
                timings_path=settings.storage.timing_log,
                audit_path=settings.storage.audit_log,
            )
        )
        return
    if args.command == "wiki":
        if args.wiki_command == "status":
            _dump(_wiki_status(settings))
            return
        if args.wiki_command == "index":
            result = _wiki_index(settings)
            _dump(result)
            if result["status"] != "ok":
                raise SystemExit(1)
            return
        _dump(
            _wiki_refresh(
                settings, namespaces=args.namespaces, delay=args.delay
            )
        )
        return

    if args.command in {"ask", "questions"}:
        try:
            if args.command == "ask":
                validate_question(args.character, args.question)
                cases = []
            else:
                cases = load_corpus(args.corpus, args.character)
            token = _token(settings)
            session = QuestionSession(
                args.character,
                lambda path: _request(path, settings=settings, token=token),
                lambda payload, timeout: _post(
                    "/v1/ask", payload, settings=settings, token=token, timeout=timeout
                ),
                allow_recon=args.allow_recon,
            )
            if args.command == "ask":
                result = session.answer(args.question)
                _dump(result)
                if result["status"] != "answered":
                    raise SystemExit(1)
            else:
                report = run_corpus(session, cases, args.output)
                _dump({
                    "status": report["status"], "output": str(args.output),
                    "cases": len(cases), "quality_review": "required",
                })
                if report["status"] != "completed":
                    raise SystemExit(1)
        except (ValidationError, OSError) as error:
            raise SystemExit(f"cannot run questions: {error}") from error
        return

    token = _token(settings)
    character = quote(args.character, safe="")
    if args.command == "state":
        _dump(
            _request(
                f"/v1/state/{character}", settings=settings, token=token
            )
        )
        return
    if args.command == "combat-report":
        payload = {"character": args.character}
        if args.operation_id:
            payload["operation_id"] = args.operation_id
        _dump(_post("/v1/session/combat/report", payload, settings=settings, token=token))
        return
    if args.command == "inventory":
        _dump(
            _post(
                "/v1/session/inventory/find",
                {"character": args.character, "query": args.query},
                settings=settings,
                token=token,
            )
        )
        return
    if args.command == "sources":
        _dump(
            _post(
                "/v1/session/sources",
                {"character": args.character},
                settings=settings,
                token=token,
            )
        )
        return
    if args.command == "stop":
        if bool(args.operation_id) != bool(args.expected_generation):
            raise SystemExit("--operation-id and --expected-generation must be supplied together")
        payload = {"character": args.character}
        if args.operation_id:
            payload.update(operation_id=args.operation_id, expected_generation=args.expected_generation)
        _dump(
            _post(
                "/v1/session/operation/stop",
                payload,
                settings=settings,
                token=token,
            )
        )
        return
    if args.command == "control":
        _dump(_post(
            "/v1/session/operation/control",
            {"character": args.character, "operation_id": args.operation_id,
             "expected_generation": args.expected_generation, "control": args.control},
            settings=settings, token=token,
        ))
        return
    if args.command == "perform":
        if not math.isfinite(args.timeout) or not 0 <= args.timeout <= MAX_WATCH_TIMEOUT_SECONDS:
            raise SystemExit(f"--timeout must be between 0 and {MAX_WATCH_TIMEOUT_SECONDS:g}")
        if not math.isfinite(args.operation_timeout) or not 0 < args.operation_timeout <= 300:
            raise SystemExit("--operation-timeout must be positive and at most 300 seconds")
        operation = _post(
            "/v1/session/perform",
            {
                "character": args.character,
                "capability": args.capability,
                "args": _perform_args(args),
                "timeout_seconds": args.operation_timeout,
                **({"expected_generation": args.expected_generation} if args.expected_generation else {}),
            },
            settings=settings,
            token=token,
        )
        if not args.wait:
            _dump(operation)
            return
        cursor = 0
        while True:
            page = _post(
                "/v1/session/operation/watch",
                {
                    "operation_id": operation["operation_id"],
                    "cursor": cursor,
                    "timeout_ms": int(args.timeout * 1_000),
                },
                settings=settings,
                token=token,
                timeout=args.timeout + 3,
            )
            for event in page["items"]:
                sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
            sys.stdout.flush()
            cursor = int(page["cursor"])
            operation = page["operation"]
            if operation["status"] in {
                "succeeded",
                "failed",
                "timed_out",
                "interrupted",
            }:
                _dump(operation)
                return

    cursor = args.cursor
    if cursor < 0:
        raise SystemExit("--cursor must be nonnegative")
    if not 0 <= args.timeout <= MAX_WATCH_TIMEOUT_SECONDS:
        raise SystemExit(
            f"--timeout must be between 0 and {MAX_WATCH_TIMEOUT_SECONDS:g}"
        )
    try:
        while True:
            query = urlencode({"cursor": cursor, "timeout": args.timeout})
            page = _request(
                f"/v1/watch/{character}?{query}",
                settings=settings,
                token=token,
                timeout=args.timeout + 3,
            )
            for event in page["events"]:
                sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
            sys.stdout.flush()
            cursor = page["cursor"]
            if args.once:
                if not page["events"]:
                    _dump(page)
                return
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
