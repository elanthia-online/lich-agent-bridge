# Lich Agent Bridge

Lich Agent Bridge (LAB) connects a live [Lich 5](https://github.com/elanthia-online/lich-5) session to local or remote AI agents without coupling the agent to a particular GemStone IV frontend.

LAB gives the agent structured game context, local game knowledge, durable item knowledge, and a narrow audited path for confirmed actions. It does not replace Lich or your frontend.

For Lich and script developers, this also provides a way to run authorized
in-game test operations and inspect their evidence—not just send commands and
assume they worked. See the [developer testing workflow](wiki/project/Developer-Testing.md).

> **Status:** Alpha. LAB is being actively dogfooded and its interfaces may still change.

## What LAB provides

- Private in-game questions with `, QUESTION`.
- Structured character state instead of screen scraping.
- Local project-wiki and GSWiki retrieval.
- Durable, character-scoped inventory and item knowledge.
- An authenticated, allowlisted action broker with explicit result reporting.
- Shell access through `labctl`.
- A typed Streamable HTTP MCP server for compatible agents.
- A frontend-state feed that clients such as Vellum Despana can render.

The answer model does not receive unrestricted game-command access. Actions travel through a separate policy layer that validates the character, session generation, command category, approval state, and reported outcome. Registered capabilities can verify supported outcomes; lower-level allowed commands are reported honestly as sent but unverified when no outcome check exists.

## Architecture

```text
GemStone IV ↔ Lich 5 ↔ lab.lic (dispatcher) → lab-bridge.lic ↔ SessionHub ↔ model and knowledge
                                                        ↕
                                                  MCP / labctl
```

The Lich adapter owns live game integration. SessionHub owns state, dialogue, capabilities, and action coordination. Frontends remain presentation layers and do not contain agent logic.

LAB's services listen only on loopback by default, but the selected answer backend still receives the assembled prompt and relevant game context. A cloud-backed model therefore sends that material beyond the local machine; choose and configure the backend according to your privacy requirements.

See [Architecture](wiki/project/Architecture.md), [Protocol](wiki/project/Protocol.md), and [Safety](wiki/project/Safety.md) for the full contracts.

## Requirements

- Lich 5
- Python 3.11 or newer
- A frontend that passes input through Lich
- A configured answer backend
- The Ruby `sqlite3` gem for durable inventory tracking

Optional Bigshot Quick and EO Hunter controllers require the companion native
Lich support described in [controller setup](examples/controllers/README.md).
With a valid, explicitly
empty controller registry, older Lich builds can still run explicit inventory
refreshes using initial ownership checks; native per-command guards are used
whenever available. See [inventory compatibility](wiki/project/Setup-and-Operations.md#install-lich-dependencies).

The current default backend is the locally installed Codex CLI using its existing ChatGPT login. A direct OpenAI Responses API adapter and an OpenAI-compatible Chat Completions adapter for local servers such as llama.cpp are also available. Named profiles select the provider, model, reasoning effort, timeout, and an optional bounded local instructions file.

Current development and live testing are Linux-oriented. The optional MCP adapter additionally requires Node.js, npm, and the native build prerequisites used by `isolated-vm`.

## Quick start

Clone the repository and install the Python package:

```bash
git clone https://github.com/elanthia-online/lich-agent-bridge.git
cd lich-agent-bridge
python3 -m venv .venv
source .venv/bin/activate
.venv/bin/pip install -e .
```

For the default Codex backend:

```bash
codex login
labctl setup
labctl config show
./scripts/run-sidecar.sh
```

`labctl setup` writes the validated configuration to
`~/.config/lich-agent-bridge/config.toml` by default. It is safe to rerun: an
existing file is preserved unless replacement is explicitly confirmed. Run
`labctl doctor` before or after startup to inspect local files, the selected
backend, and loopback service readiness without contacting the game.

Copy or symlink these files into the active Lich `scripts` directory:

- `lich/lab.lic`
- `lich/lab-bridge.lic`
- `lich/lab-inventory.lic`
- `lich/lich-state-core.rb`
- `lich/lab-controller-registry.rb`
- `lich/lab-controller-controls.rb`
- `lich/lab-combat-report.rb`
- `lich/lab-test-runner.rb`
- `lich/lab-controllers.json`

Each file needs its own entry in the active scripts directory because Lich resolves runtime dependencies there, even when `lab.lic` itself is a symlink.

Optional combat reporting reuses Lich's `Combat::Recorder`; LAB does not start
or replace it. With post-commit receipt protocol 1 and Hunter trial context
support, `labctl combat-report CHARACTER --operation-id ID` reads the retained
report after verified refuge return. Agents use `combat.report` in questions or
`lab.combat_report` through MCP. Omitting the ID selects the latest controller
operation, not a general hunt-history search. Missing support/data is reported
as unavailable. This first slice reads the conventional per-character
`combat_stats.db` location under Lich's data directory; custom Recorder paths
are not discovered. See the [combat-reporting contract](wiki/project/Combat-Reporting-Plan.md)
for prerequisites, limits, and the bounded live acceptance already completed.

The bundled controller manifest is empty. Personal combat routines, hunting
profiles, character builds, and equipment configuration are not distributed or
required. The registry supports separately reviewed local extensions; installing
LAB does not select a gameplay strategy for your character.

Start the bridge in game:

> **Safety default:** the local action path and allowlisted auto-approval start enabled. Use `;lab actions off` to keep the conversational bridge running without action execution, or `;lab stop` to stop the bridge completely.

```text
;lab
;lab status
, what just happened in this room?
```

Use `;lab stop` to stop the bridge and remove its hooks.

The user-service installer renders portable units for the current clone and
active configuration. See [Setup and Operations](wiki/project/Setup-and-Operations.md).

## In-game interface

Quick conversation:

```text
, QUESTION
```

Management:

```text
;lab help
;lab status
;lab state
;lab scripts
;lab inventory
;lab alerts
;lab last
;lab sources
;lab context
;lab forget
;lab actions on
;lab actions off
;lab approve
;lab approve auto
;lab approve auto off
;lab operation stop
;lab recover
;lab stop
```

For a retained failed controlled-outing handoff, `;lab recover` lists the exact run ID.
After restoring the character, `;lab recover RUN_ID confirm` verifies refuge,
original equipment and released ownership before acknowledging that one run.
It sends no game commands and does not turn the failed test into a pass.
See [recovery checks](wiki/project/Controller-Controls.md#player-confirmed-recovery-after-a-lab-restart).

The default catalog contains five capabilities:

- `character.recon`: fixed INFO/SKILLS inspection with verified observations.
- `travel.go2`: exact-room native go2 travel with bounded execution and verified
  arrival; requires compatible guarded Lich/go2 builds and explicit player authorization.
- `item.audit`: attributed diagnostics for an exact current item.
- `room.loot`: a bounded ELoot sweep with admission and outcome checks.
- `hunt.prepare`: unavailable without a configured character profile; no profiles
  are bundled.

The controller manifest is empty. Retrieve the live catalog and its schemas
instead of hard-coding availability:

- MCP: `lab.capabilities`
- HTTP: `POST /v1/session/capabilities`

An optional [trusted script-test pilot](wiki/project/Developer-Testing.md#trusted-script-test-pilot)
uses this same controller interface for short, explicitly registered non-combat
suites. Offline `labctl tests prepare` prints a pinned registration for review;
it does not install or enable one. The example is a harmless lifecycle probe,
not a sandbox or an unattended gameplay test campaign.
For that pilot, also install `lich/lab-test-runner.lic` as a real file alongside
the helper and reviewed suite files; suite pinning rejects symlink substitutions.

An optional [EO Hunter trial campaign](examples/controllers/README.md#eo-hunter-trial-campaign)
can execute a short sequence of private, reviewed combat routines at game speed,
record structured evidence, and return to a player-selected refuge before the
agent evaluates results. Installing LAB alone does not enable it; the shipped
controller registry remains empty.

Questions use a bounded evidence loop, not a list of question keywords. The model
can answer from the supplied context or request current state, character
observations, recorded item facts, or configured wiki research. `knowledge.search`
discovers compact source handles; `knowledge.read` reads a selected source or
section with bounded continuations. Reference mechanics, selected-character
notes, and development documentation have separate search scopes. LAB validates
those requests; the model cannot supply arbitrary commands, paths, or URLs.

Known limitation: multi-part questions can find the right page but miss the
specific interaction-rule passage. Further passage-selection tuning is tracked
in [issue #7](https://github.com/elanthia-online/lich-agent-bridge/issues/7).

`character.read` reuses recent same-session INFO/SKILLS observations and can
request missing or older-than-two-minute categories through the existing recon
and approval gates. `actions off` prevents this refresh. Turning actions back on
does not restore auto-approval: use `;lab approve` for a pending request or
explicitly enable `;lab approve auto`. Only completed, timestamped observations
update the configured database; historical values and missing formulas remain
labeled as such. Inventory searches read recorded dossiers, not a new live
container scan. See [Evidence gathering](wiki/project/Evidence-Gathering-Arc.md).

Direct answers need one model call. Evidence gathering allows at most three
rounds, four requests per batch, and eight requests total under the same question
deadline. `;lab forget` invalidates the question and revokes its pending recon;
commands already dispatched cannot be unsent. `;lab sources` shows the supplied
references and diagnostics.

Developers can use `labctl ask CHARACTER "QUESTION"` to exercise that same
pipeline from a shell, or `labctl questions CHARACTER CORPUS.json --output
/PRIVATE/PATH/results.json` for sequential question cases. Both default to
server-enforced read-only questions; explicit `--allow-recon` permits only the
existing independently gated INFO/SKILLS path. They require a fresh selected
session, do not log characters in, and preserve normal dialogue. Keep corpora and
results private. See [question testing](wiki/project/Developer-Testing.md#direct-questions-and-private-question-corpora)
for the format, safety boundary, and manual quality-review requirements.

Evidence allowances are configurable per agent profile: defaults are 12,000
characters per result and 36,000 in the evidence context supplied on each turn.
A question-local workspace retains bounded results outside that context: later
reads can displace earlier discovery results, and repeating a request reactivates
cached evidence without repeating game commands. Source reporting distinguishes
discovery snippets from read passages and lists only the final context's sources.
Larger
allowances can improve multi-source answers but increase model input and may
send more private context to the configured backend. See
[evidence settings](wiki/project/Setup-and-Operations.md#evidence-allowances).

## Shell interface

After installing the package, use the `labctl` entry point:

```bash
labctl config path
labctl config show
labctl doctor
labctl status
labctl state Yourcharacter
labctl watch Yourcharacter
labctl inventory Yourcharacter staff
labctl sources Yourcharacter
labctl perform Yourcharacter item.audit --item-id 12345 --method look --method inspect --wait
labctl stop Yourcharacter
```

State, watch, inventory, and sources operations are read-only. `sources` shows
references supplied to the model for the character's most recent answer and
retrieval diagnostics, not the private prompt. `perform` uses the same capability and action policy as
every other caller.

## Optional MCP adapter

Build and run the local MCP service:

```bash
cd mcp
npm ci
npm run build
cd ..
./scripts/run-mcp.sh
```

The default endpoints are:

- SessionHub: `http://127.0.0.1:18765`
- MCP: `http://127.0.0.1:18766/mcp`

Register it with Codex:

```bash
codex mcp add lab --url http://127.0.0.1:18766/mcp
```

Restart the client after changing MCP registration. The MCP server remains loopback-only and obtains its SessionHub credential from LAB's private state directory; the credential is not exposed as a tool argument.

See [the MCP README](mcp/README.md) for its tools and bounded TypeScript executor.

## Current configuration

The primary configuration is the versioned, validated TOML schema at
`~/.config/lich-agent-bridge/config.toml`. Use `labctl setup` to create or
revise it, `labctl config path` to identify the active file, and `labctl config
show` for a resolved, secret-free view. `--config PATH` selects another file
for LAB command-line entry points.

Existing environment settings remain higher-precedence compatibility
overrides. Common overrides are:

- `LAB_CONFIG` — select a settings file instead of the XDG default.
- `LAB_HOST`, `LAB_PORT`, and `LAB_MCP_PORT` — loopback service endpoints;
  SessionHub defaults to port `18765` and MCP to `18766`.
- `LAB_BACKEND` — `codex` by default or `openai`.
- `LAB_CODEX_MODEL` — optional Codex model override.
- `OPENAI_API_KEY` — required only for the direct OpenAI backend.
- `LAB_MODEL` — optional model override for the direct OpenAI backend.
- `LAB_GSWIKI_DB` — optional path to a local GSWiki SQLite mirror.
- `LAB_WIKI_ROOT` — optional path to the curated Markdown wiki.
- `LAB_ACTION_TOKEN_FILE` — optional private token-file override.
- `LAB_INVENTORY_DB` — optional durable inventory-database override.
- `LAB_LICH_DATA_DIR` and `LAB_GAME` — locate an existing Lich inventory ledger when `LAB_INVENTORY_DB` is not set.

Persistent runtime state defaults to `~/.local/state/lich-agent-bridge/`. Generated local knowledge defaults to `.lab-cache/`. Credentials, tokens, raw logs, and character-private data must not be committed.

### Configuration milestone status

A unified settings file, guided `labctl setup`, redacted inspection, and local
`labctl doctor` diagnostics are implemented. The selected profile now drives
the Codex CLI, OpenAI Responses, or an OpenAI-compatible Chat Completions
adapter; the latter is suitable for llama.cpp-style local servers and does not
assume that they implement the Responses endpoint. Custom instructions are
read from one bounded UTF-8 regular file and are appended beneath LAB's
non-overridable safety and evidence-tool instructions.

For example, after `labctl setup`, a local profile can select the built-in
`llama_cpp` provider, set its server URL (normally
`http://127.0.0.1:8080/v1`), and name the loaded model. No credential is
required unless the local server itself requires one. Local retrieval and
ephemeral dialogue continuity are complete; bounded online-reference fallback
continues in the fixed order recorded in the
[configuration and knowledge completion arc](wiki/project/Configuration-and-Knowledge-Arc.md).

## Knowledge

LAB searches reviewed project notes and the configured local GSWiki mirror
first. If that mirror is missing, stale, or does not produce a sufficiently
relevant result, the default `when_needed` policy performs one bounded live
GSWiki lookup. Live excerpts retain their GSWiki URL, revision, and retrieval
time; a small local cache avoids repeating the same request.

Use `labctl wiki status` to inspect mirror path, size, schema health, last sync,
freshness threshold, and passage-index readiness/coverage. `labctl wiki refresh` builds a replacement mirror and
only swaps it in after a successful sync, preserving the previous usable mirror
on failure.

`labctl wiki index` adds or rebuilds the local passage index without downloading
pages or making old sources fresh. Explicit indexing regenerates derived tables
from the retained pages, including repair of damaged derived content; ordinary
sync reuses unchanged snapshots. Keep mirror writers offline while it runs;
it requires a DELETE-journal mirror (the sync default) and enough free disk space
for a replacement database. It does not convert an active WAL database.
Subsequent syncs maintain the index. Missing or unusable indexes fall back to
page search; questions never trigger database migration.

Indexed research exposes matching passages through the same `knowledge.search`
and `knowledge.read` tools. Ranges carry revision and normalized-text identity,
so replaced pages cannot silently reinterpret old offsets. Default retrieval is
local lexical search and does not expand MediaWiki templates.
For an offline synthetic comparison, run
`python3 scripts/benchmark-wiki-retrieval.py --iterations 5`.
See the [passage-index plan](wiki/project/Passage-Index-Plan.md) for scope and limits.
Selected pages also support focused passage recovery and contiguous coverage
windows within the existing read limit. Optional local semantic source reranking
is available through the `semantic` installation extra and one
`knowledge.semantic_model_directory` setting. It protects explicitly named
references, caches bounded window vectors, and falls back to lexical order when
unavailable or over allowance. Nothing downloads automatically; base installation
remains dependency-free. See [semantic setup and limits](wiki/project/Semantic-Reranking.md)
and the [coverage experiments](wiki/project/Retrieval-Coverage-Expansion.md).

General-web fallback is separately opt-in. Set a selected profile's
`web_search = true`, choose `general_web_provider = "brave"`, and provide only
the credential environment-variable name (normally `BRAVE_SEARCH_API_KEY`) in
`general_web_credential_env`. No credential value is written to settings.

The versioned wiki records public architecture, safety contracts, Lich behavior,
and integration references. Keep character notes, builds, equipment, routines,
private instructions, logs, and databases in a separate user-controlled location.
An optional private Markdown root can supply personal knowledge without adding
it to the public source tree:

- [Wiki home](wiki/Home.md)
- [Project documentation](wiki/project/README.md)
- [Lich documentation](wiki/lich/README.md)
- [GemStone IV documentation](wiki/gsiv/README.md)
- [Reference sources](wiki/reference/README.md)

Build or refresh the optional local GSWiki mirror with:

```bash
lab-gswiki
```

Build the ignored local Lich community-script corpus with:

```bash
lab-lich-corpus
```

## Development

Run the Python suite:

```bash
PYTHONPATH=src python3 -m unittest discover -v
```

Run the MCP checks:

```bash
cd mcp
npm test
npm run typecheck
npm run check:sdk-types
npm run build
```

Check repository-local links in the Markdown documentation:

```bash
python3 scripts/check-docs.py
```

The core Ruby bridge tests require a Ruby runtime compatible with the installed Lich version:

```bash
ruby tests/lab_dispatcher_test.rb
ruby tests/lab_bridge_test.rb
ruby tests/lab_go2_travel_test.rb
ruby tests/lab_controller_registry_test.rb
ruby tests/lab_controller_controls_test.rb
ruby tests/lab_controller_control_binding_test.rb
ruby tests/lab_test_runner_test.rb
ruby tests/lab_inventory_test.rb
bash tests/play_gemstone_detach_test.sh
```

Optional ELoot compatibility harnesses require `ELOOT_SOURCE` to identify the
explicit external `eloot.lic` checkout; they are not part of the self-contained
core suite. Consult [the patch notes](patches/README.md) before running them.

Before contributing, read [AGENTS.md](AGENTS.md). Before changing a `.lic` script, Lich API usage, XML state handling, container tracking, or a frontend boundary, also read [Lich Authoring References](wiki/lich/Lich-Authoring-References.md).

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution and verification
requirements, and [SECURITY.md](SECURITY.md) for private vulnerability reporting
and deployment precautions.

## License

Lich Agent Bridge is licensed under the [GNU General Public License v3.0 only](LICENSE).
