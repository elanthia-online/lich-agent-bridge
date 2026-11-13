#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
home_dir="${HOME:?HOME must identify the user installing the services}"
config_root="${XDG_CONFIG_HOME:-${home_dir}/.config}"
settings_file="${LAB_CONFIG:-${config_root}/lich-agent-bridge/config.toml}"
render_only=false

usage() {
  printf '%s\n' \
    'Usage: scripts/install-user-services.sh [--config PATH] [--render-only]' \
    '' \
    'Renders portable user units into XDG_CONFIG_HOME/systemd/user.' \
    'By default, reloads systemd and enables/starts both services.' \
    '--render-only writes the units without invoking systemctl.'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      if [[ $# -lt 2 ]]; then
        printf '%s\n' '--config requires a path' >&2
        exit 2
      fi
      settings_file="$2"
      shift 2
      ;;
    --render-only)
      render_only=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${config_root}" != /* ]]; then
  printf 'XDG_CONFIG_HOME must be an absolute path: %s\n' "${config_root}" >&2
  exit 2
fi

service_dir="${config_root}/systemd/user"
mkdir -p -- "${service_dir}"
chmod 700 -- "${service_dir}"

runtime_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
for runtime_command in python3 node npm codex claude; do
  if resolved_command="$(command -v "${runtime_command}" 2>/dev/null)" && [[ "${resolved_command}" == /* ]]; then
    command_directory="${resolved_command%/*}"
    case ":${runtime_path}:" in
      *":${command_directory}:"*) ;;
      *) runtime_path="${command_directory}:${runtime_path}" ;;
    esac
  fi
done

render_unit() {
  local template="$1"
  local destination="$2"
  python3 - "${template}" "${destination}" "${project_dir}" "${settings_file}" "${runtime_path}" <<'PY'
import os
import sys
import tempfile
from pathlib import Path


template, destination, project, config, runtime_path = sys.argv[1:]


def escaped(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise SystemExit("service template values cannot contain newlines")
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def escaped_path(value: str) -> str:
    return escaped(value).replace(" ", "\\x20").replace("\t", "\\x09")


values = {
    "@PROJECT_DIR@": escaped(str(Path(project).resolve())),
    "@WORKING_DIRECTORY@": escaped_path(str(Path(project).resolve())),
    "@CONFIG_FILE@": escaped(str(Path(config).expanduser().resolve())),
    "@EXEC_PATH@": escaped(runtime_path),
}
payload = Path(template).read_text(encoding="utf-8")
for marker, value in values.items():
    payload = payload.replace(marker, value)
remaining = [marker for marker in values if marker in payload]
if remaining:
    raise SystemExit(f"unresolved service template marker: {remaining[0]}")

target = Path(destination)
descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
temporary = Path(temporary_name)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)
finally:
    temporary.unlink(missing_ok=True)
PY
}

render_unit \
  "${project_dir}/packaging/systemd/lich-agent-bridge.service.in" \
  "${service_dir}/lich-agent-bridge.service"
render_unit \
  "${project_dir}/packaging/systemd/lich-agent-bridge-mcp.service.in" \
  "${service_dir}/lich-agent-bridge-mcp.service"

printf 'Installed user units in %s\n' "${service_dir}"

if [[ "${render_only}" == true ]]; then
  exit 0
fi

systemctl --user daemon-reload
systemctl --user enable --now lich-agent-bridge.service lich-agent-bridge-mcp.service
