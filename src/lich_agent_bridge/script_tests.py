"""Offline preparation of explicitly trusted, revision-pinned script suites.

This module reads developer-selected local files and returns a controller entry.
It never installs a registration, starts Lich, enables actions, or runs scripts.
Digests detect later changes; they neither sandbox Ruby nor discover undeclared
dynamic dependencies or eliminate concurrent local-file mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from .errors import ConfigurationError


MAX_MANIFEST_BYTES = 65536
_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_SCRIPT = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_ARG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\Z")
_CHARACTER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_ROOM = re.compile(r"[0-9]{1,20}\Z")
_FIELDS = frozenset({"room_id", "right_hand_id", "left_hand_id", "health",
                     "mana", "spirit", "dead", "stunned"})
_RUNNER_FILES = ("lab-test-runner.lic", "lab-test-runner.rb")
_SCRIPT_SUFFIX = re.compile(r"\.(?:lic|lich|rb|cmd|wiz)(?:\.gz|\.Z)?\Z", re.IGNORECASE)


def _object(value: Any, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ConfigurationError(f"{label} must contain exactly {', '.join(sorted(keys))}")
    return value


def _string(value: Any, pattern: re.Pattern, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ConfigurationError(f"{label} is invalid")
    return value


def _relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 240:
        raise ConfigurationError(f"{label} must be a bounded relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or _SEGMENT.fullmatch(part) is None for part in parts):
        raise ConfigurationError(f"{label} must be a canonical scripts-root-relative path")
    return value


def validate_script_suite(raw: Any) -> dict[str, Any]:
    """Validate the shared fixed-case schema, without evaluating expressions."""
    suite = _object(raw, {"version", "id", "script", "files", "cases", "limits"}, "suite")
    if type(suite["version"]) is not int or suite["version"] != 1:
        raise ConfigurationError("suite.version must be 1")
    _string(suite["id"], _ID, "suite.id")
    script = _string(suite["script"], _SCRIPT, "suite.script")
    if script == "lab-test-runner":
        raise ConfigurationError("suite.script cannot be the runner itself")
    files = suite["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 64:
        raise ConfigurationError("suite.files requires 1–64 explicit dependencies including target")
    paths = [_relative(item, "suite.files") for item in files]
    if len({item.casefold() for item in paths}) != len(paths):
        raise ConfigurationError("suite.files contains duplicate paths")
    targets = [path for path in paths if PurePosixPath(path).name == script + ".lic"]
    if len(targets) != 1:
        raise ConfigurationError("suite.files must include exactly one target .lic path")
    cases = suite["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 20:
        raise ConfigurationError("suite.cases requires 1–20 declared cases")
    seen = set()
    for index, raw_case in enumerate(cases):
        label = f"suite.cases[{index}]"
        case = _object(raw_case, {"id", "args", "assertions"}, label)
        case_id = _string(case["id"], _ID, f"{label}.id")
        if case_id == "all" or case_id in seen:
            raise ConfigurationError("case IDs must be unique and cannot be all")
        seen.add(case_id)
        args = case["args"]
        if not isinstance(args, list) or len(args) > 32:
            raise ConfigurationError(f"{label}.args permits at most 32 fixed tokens")
        for arg in args:
            _string(arg, _ARG, f"{label}.args")
        assertions = case["assertions"]
        if not isinstance(assertions, list) or not 1 <= len(assertions) <= 32:
            raise ConfigurationError(f"{label}.assertions requires 1–32 named checks")
        for assertion in assertions:
            if not isinstance(assertion, dict):
                raise ConfigurationError("assertion must be an object")
            operation = assertion.get("op")
            if not isinstance(operation, str) or operation not in {"equals", "unchanged"}:
                raise ConfigurationError("assertion.op must be equals or unchanged")
            _object(assertion, {"field", "op", "value"} if operation == "equals" else {"field", "op"}, "assertion")
            if not isinstance(assertion["field"], str) or assertion["field"] not in _FIELDS:
                raise ConfigurationError("assertion.field is unsupported")
            if operation == "equals":
                value = assertion["value"]
                if type(value) not in {str, bool, int, float}:
                    raise ConfigurationError("assertion.value must be a non-null scalar")
                if isinstance(value, str) and len(value) > 256:
                    raise ConfigurationError("assertion.value is too long")
                if isinstance(value, float) and not math.isfinite(value):
                    raise ConfigurationError("assertion.value must be finite")
    limits = _object(suite["limits"], {"case_seconds", "run_seconds", "cleanup_seconds"}, "suite.limits")
    for key, value in limits.items():
        if type(value) not in {int, float} or value <= 0 or (isinstance(value, float) and not math.isfinite(value)):
            raise ConfigurationError(f"suite.limits.{key} must be positive and finite")
    if not limits["case_seconds"] <= limits["run_seconds"] <= 20 or limits["cleanup_seconds"] > 3:
        raise ConfigurationError("suite limits require case <= run <= 20 seconds and cleanup <= 3 seconds")
    return suite


def _contained_file(root: Path, relative: str) -> Path:
    _relative(relative, "file")
    candidate = root / relative
    cursor = root
    for component in PurePosixPath(relative).parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ConfigurationError(f"registered file path contains a symlink: {relative}")
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not stat.S_ISREG(resolved.stat().st_mode):
            raise ConfigurationError(f"registered path is not a contained regular file: {relative}")
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(f"registered file is missing or unreadable: {relative}") from error
    return resolved


def _exact_script(root: Path, script: str, approved: str, *, runner=False) -> None:
    """Reject prefix fallback and ambiguous root/custom copies before registration."""
    directories = [root]
    custom = root / "custom"
    if custom.is_symlink():
        raise ConfigurationError("custom script directory cannot be a symlink")
    if custom.is_dir():
        directories.append(custom)
        for child in custom.iterdir():
            if child.is_dir():
                if child.is_symlink():
                    raise ConfigurationError("custom script subdirectories cannot be symlinks")
                directories.append(child)
    matches = []
    for directory in directories:
        for candidate in directory.iterdir():
            basename = _SCRIPT_SUFFIX.sub("", candidate.name)
            if basename.casefold() != script or basename == candidate.name:
                continue
            relative = candidate.relative_to(root).as_posix()
            if runner and relative == "lab-test-runner.rb":
                continue  # Required helper, not an alternate launch target.
            _contained_file(root, relative)
            matches.append(relative)
    if matches != [approved]:
        raise ConfigurationError(f"script {script} has no unique exact approved .lic file; shadowing and prefix fallback are not permitted")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("suite JSON contains duplicate keys")
        result[key] = value
    return result


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_script_suite(manifest_path: Path, scripts_dir: Path, character: str, room_id: str) -> dict[str, Any]:
    """Return one reviewed controller entry; caller decides where/if to install it.

    Relative manifest paths resolve under the explicitly supplied scripts root.
    All pins refer to canonical relative paths, never deployment-machine paths.
    """
    _string(character, _CHARACTER, "character")
    _string(room_id, _ROOM, "room_id")
    root = Path(scripts_dir).resolve()
    if not root.is_dir():
        raise ConfigurationError("scripts_dir must be an existing directory")
    supplied = Path(manifest_path)
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError as error:
        raise ConfigurationError("suite manifest must be inside scripts_dir") from error
    if PurePosixPath(relative).suffix != ".json":
        raise ConfigurationError("suite manifest must be a JSON file")
    manifest = _contained_file(root, relative)
    try:
        with manifest.open("rb") as handle:
            content = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES:
            raise ConfigurationError("suite manifest exceeds 64 KiB")
        raw = json.loads(content, object_pairs_hook=_unique_object)
        suite = validate_script_suite(raw)
        target = next(item for item in suite["files"] if PurePosixPath(item).name == suite["script"] + ".lic")
        _exact_script(root, suite["script"], target)
        _exact_script(root, "lab-test-runner", "lab-test-runner.lic", runner=True)
        files = dict.fromkeys([relative, *_RUNNER_FILES, *suite["files"]])
        hashes = {name: _digest(_contained_file(root, name)) for name in files}
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise ConfigurationError("suite or declared files cannot be prepared") from error
    revision = hashlib.sha256(content).hexdigest()
    # Bind the manifest pin to the bytes actually validated, not a later reread.
    hashes[relative] = revision
    suite_id = suite["id"]
    return {
        "name": f"test-{suite_id}", "script": "lab-test-runner",
        "summary": f"Run approved non-combat script suite {suite_id} and verify named assertions.",
        "characters": [character], "result_global": "$lab_test_result",
        "signal_global": "$lab_test_cancel", "lanes": ["movement", "combat"],
        "owner_scripts": ["lab-test-runner", suite["script"]],
        "safe_handoff": {"kind": "room", "room_id": room_id},
        "capability_action": "start",
        "actions": [{
            "name": "start", "kind": "launch",
            "command_template": f"lab-test {suite_id} {{revision}} {{case_id}}",
            "script_args_template": f"{suite_id} {{revision}} {{case_id}}",
            "launch_mode": "start",
            "policy": {"category": "configuration", "confirmation_required": True},
            "parameters": [
                {"name": "revision", "type": "enum", "values": [revision]},
                {"name": "case_id", "type": "enum", "values": [case["id"] for case in suite["cases"]] + ["all"]},
            ],
        }],
        "test_suite": {"manifest": relative, "files": hashes},
    }
