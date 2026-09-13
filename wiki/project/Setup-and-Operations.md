# Setup and operations

LAB is installed from source alongside an existing Lich 5 installation. It does
not replace Lich login, stored accounts, or frontend configuration.

## Install the Python service

From the cloned repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
labctl setup
labctl config show
labctl doctor
./scripts/run-sidecar.sh
```

Set up the selected model backend independently. The default Codex CLI backend
uses the existing CLI login. API backends read credentials from the configured
environment-variable name. Local compatible servers require their own running
model endpoint.

The default settings file is
`~/.config/lich-agent-bridge/config.toml`; runtime state is under
`~/.local/state/lich-agent-bridge/`. Use `labctl config path` to inspect the
resolved location. A `--config PATH` or supported environment override can select
a different installation. Keep private data outside the public source checkout.

## Install Lich dependencies

Copy or symlink each file into the active Lich scripts directory:

- `lich/lab.lic`
- `lich/lab-bridge.lic`
- `lich/lab-inventory.lic`
- `lich/lich-state-core.rb`
- `lich/lab-controller-registry.rb`
- `lich/lab-controller-controls.rb`
- `lich/lab-combat-report.rb`
- `lich/lab-test-runner.rb`
- `lich/lab-controllers.json`

Lich resolves these dependencies in its scripts directory even when the
dispatcher itself is symlinked. Install a compatible Ruby SQLite dependency
for inventory tracking.

When deliberately switching a stopped bridge to another build through symlinks,
the bridge resolves the registry's canonical path before requiring it. This
avoids reusing the old build's registry from Ruby's require cache. It is not a
general hot-reload guarantee for files edited in place; stop active operations
and keep the sidecar, bridge dependencies and private manifest compatible.

The public controller manifest is empty. Personal combat scripts, hunt profiles,
and character policy are neither bundled nor required for the bridge.
The optional [Quick trial example](../../examples/controllers/README.md) requires
explicit registration and compatible Bigshot/native Lich lifecycle support.

Explicit inventory enhancive/charge refreshes retain compatibility with older
Lich versions without native execution guards only when the loaded controller
registry is valid and explicitly empty. The bridge must still be available:
refreshes claim the inventory lane and check for competing owners before starting.
This legacy path does not provide per-command revocation; avoid starting other
equipment/combat/movement scripts during the refresh. Registering any controller
requires native guards for these refreshes. Missing or malformed registry state
does not enable the fallback. When native guards exist, they are always used,
including with an empty registry. Passive inventory observation is unchanged.

The optional trusted script-test pilot also needs `lich/lab-test-runner.lic`.
Use real copies of the runner pair and reviewed suite files for that pilot:
revision pinning rejects symlink substitutions. Merely installing these files
does not register a suite or grant test execution authority.

## Start deliberately

Starting the bridge is a live-game action. In an authorized session:

```text
;lab
;lab status
;lab actions off
, what information is available about this room?
```

Action execution and allowlisted auto-approval currently start enabled.
`;lab actions off` prevents command execution while retaining conversation;
`;lab stop` exits the bridge. Review [Safety](Safety.md) before enabling actions.

## Optional services and MCP

The user-service installer renders templates using the current clone and
configuration rather than a maintainer's paths. Inspect its `--help` and the
rendered units before starting them.

For MCP, from the source checkout:

```sh
cd mcp
npm ci
npm run build
cd ..
./scripts/run-mcp.sh
```

SessionHub defaults to loopback port 18765 and MCP to port 18766.
Do not expose these services to a network as an incidental troubleshooting step.
The optional frontend-state adapter is separate from agent startup.

## Knowledge and storage

Use setup to select an optional private Markdown root and GSWiki mirror.
`labctl wiki status` diagnoses the mirror; `labctl wiki refresh` refreshes it.
General-web fallback is separately opt-in. Downloaded references and script
corpora remain local data subject to their own licenses.

Local semantic source reranking is also opt-in: install the `semantic` extra,
provide the pinned model artifacts yourself, and set
`knowledge.semantic_model_directory`. Setup offers this optional field; doctor
checks readiness without inference. Default lexical retrieval remains available.
See [semantic setup, costs, and fallback](Semantic-Reranking.md).

Configure `LAB_INVENTORY_DB` or the supported Lich-data settings to read the
same private inventory ledger used by the bridge. Different data paths can make
the service appear to have no inventory even when the Lich script has recorded
it. Verify resolved paths with doctor rather than copying another user's path.

## Diagnose without changing the game

Check service health, resolved configuration, selected backend, local files,
and existing logs first. Status/source/timing queries do not require new game
commands. Logins, script reloads, snapshots that issue commands, and live
operations require the user's authorization. Keep raw diagnostic data private.

After authorized login, `labctl ask CHARACTER "QUESTION"` uses the normal question
pipeline from a shell. `labctl questions CHARACTER CORPUS.json --output
/PRIVATE/PATH/results.json` runs bounded sequential cases and saves private results.
Questions default to game-read-only; explicit `--allow-recon` permits existing
INFO/SKILLS gates without changing global policy. Both commands use the selected
settings and private token, and pin the admitted character session. See
[developer question testing](Developer-Testing.md#direct-questions-and-private-question-corpora).

## Evidence allowances

Edit the selected profile in the settings file reported by `labctl config path`:

```toml
[profiles.default]
# Keep the profile's existing provider/model settings.
evidence_result_chars = 12000
evidence_total_chars = 36000
```

These optional fields also appear in `labctl config show` and saved setup
configuration. Existing files without them use the values above. Profiles can
use smaller allowances for a local model's limited context, or larger ones for
multi-source questions. The per-result range is 3,000–100,000 characters; total
must be at least per-result plus 2,400 characters for omission notices, and no
more than 300,000. These are character counts, not model-token guarantees.

The total is the working evidence context per model turn. Bounded results remain
in a question-local workspace even when displaced from that context; cached
reactivation does not repeat recon. Source text is read in bounded pages rather
than increasing allowances until entire documents fit. Three rounds, four
requests per batch, eight requests total, and the question deadline still apply.

Higher limits may send more private observations to the selected backend and
increase input cost, latency, and context usage. Keep the initial context,
instructions, output, and model context capacity in mind when choosing them.
The change does not increase evidence rounds, permit additional game commands,
or override action approval. Apply settings at a deliberate service restart;
editing configuration alone does not alter an already running sidecar.
