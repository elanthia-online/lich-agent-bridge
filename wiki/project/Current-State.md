# Current implementation

LAB is an alpha source-installed project. This page describes implemented
interfaces, not a guarantee about any user's installation or live character.

## Available

- A short-lived Lich dispatcher and long-lived frontend-independent bridge.
- Structured world snapshots, event watches, and provenance-aware context.
- Private comma-prefixed questions and bounded temporary dialogue.
- Validated TOML configuration, guided setup, named model profiles, and doctor.
- Codex CLI, OpenAI Responses, and compatible Chat Completions model adapters.
- Optional local Markdown, GSWiki mirror, live GSWiki, and opt-in web fallback.
- Private inventory/item dossiers and timestamped character observations.
- A bounded evidence loop with fixed INFO/SKILLS recon behind existing gates.
- Authenticated actions, registered operations, CLI, and typed MCP access.
- An opt-in trusted non-combat script-test pilot: offline pinned registration,
  fixed cases/assertions, exact-operation stop, owned-child cleanup, and private
  reports. See [scope and verification gates](Script-Test-Pilot-Plan.md).
- An optional frontend-state presentation adapter.

No character notes, personal scripts, combat strategies, or hunting profiles are
bundled. An empty controller registry is intentional. Local extensions need
reviewed policy and tests; installing LAB does not establish a gameplay routine.

## Boundaries

Conversation cannot supply arbitrary game commands, launch arbitrary scripts,
query arbitrary SQL, or browse arbitrary local files. Evidence requests are
validated application operations. Action execution and approval remain separate
from model selection.

Durable facts come from attributed observations, not model answers. Historical
knowledge remains historical. Sending a command does not prove it succeeded.

## Verification

Use the checked-in Python, Ruby, and MCP suites for isolated verification.
External-script patch harnesses additionally require the corresponding upstream
script source. A passing local suite is not a live-game smoke test or a hosted CI
result. Record those separately for the exact release candidate.

See [Setup](Setup-and-Operations.md), [Developer testing](Developer-Testing.md),
and [Release checklist](Public-Release-Readiness.md).
