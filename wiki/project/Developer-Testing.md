# Developer testing with LAB

LAB gives Lich and script developers a structured interface to the actual game
environment. It does not grant an agent permission to invent commands, launch
arbitrary scripts, or experiment on a character without consent.

## Recorded combat evidence

The optional [combat-reporting interface](Combat-Reporting-Plan.md) attaches
Recorder evidence to native Hunter trials after verified refuge return. Use
`labctl combat-report CHARACTER --operation-id ID`, MCP `lab.combat_report`, or
the conversational agent's `combat.report` tool. The ID is the LAB operation
ticket, not a script name, creature ID, or action ID. Omit it for the latest
controller operation; a running latest operation returns unavailable, not an
older result disguised as current.

This requires a player-started Recorder with post-commit receipt protocol 1,
native trial recording context, and SQLite's statement-timeout support. LAB
does not enable the recorder, change its subscriptions, or parse terminal
reports. Results retain generation and operation attribution and are historical.
They live only as long as retained SessionHub operation history.

Compare observed damage, defenses, native trial outcomes, and elapsed time;
do not equate attack-record count with cast count, or net resource change with
spell cost. Partial/unknown evidence must remain unknown. The first slice does
not support ordinary hunt-history search, status/flare causality, or automatic
"best spell" rankings.

Offline tests: `ruby tests/lab_combat_report_test.rb` uses synthetic SQLite
fixtures. `LAB_TEST_LICH_CORE=/path/to/reviewed/lich-checkout ruby
tests/lab_combat_recorder_integration_test.rb` exercises the actual Recorder
contract without connecting to a game. A player-authorized single-target live
trial on 2026-09-11 verified refuge return and matched its retained report to
Recorder rows and game output. See the [acceptance scope and
limits](Combat-Reporting-Plan.md#live-acceptance--2026-09-11); this does not verify
all multi-target, group, or delayed-effect cases in live play.

## Start with an isolated reproduction

Use the Python state, policy, operation, and HTTP tests plus the Ruby fake-Lich
harnesses before a live test. They exercise malformed input, state generations,
unknown identifiers, ownership conflicts, timeouts, rejection, and kill switches
without a game connection or model account. See [CONTRIBUTING](../../CONTRIBUTING.md).

Define success evidence before execution: what exact state or attributed output
must appear, what failure looks like, and how the character/item is restored.
“Command sent,” “script starting,” and “no exception” are not successful game
outcomes. Do not add retries that might repeat a non-idempotent game action.

## Player-authorized live sequence

1. Obtain authorization for the exact character, operation, resources, and
   recovery plan. Prefer a safe room and a harmless item. Do not begin with a
   hunt or combat-controller test.
2. Call `lab.snapshot` and inspect `character`, `generation`, `freshness`, room,
   resources, hands, and ownership. Stop if absent, stale, or inconsistent with
   the authorized setup.
3. Call `lab.capabilities` for that character. Use an advertised capability and
   its argument schema; discovery is not authorization and availability is not
   a guarantee that admission will succeed.
4. Resolve any target through current inventory/state. Use its exact current
   numeric object ID, not a remembered noun or an ID from a previous session.
5. Call `lab.perform` once. Direct MCP perform returns the stable operation
   ticket immediately; poll that ticket with `lab.operation_watch` until it is
   terminal. CLI `labctl perform --wait` supports a synchronous shell workflow.
   Do not repeat a request just because the caller lost its connection.
6. Inspect `status`, `explanation`, `evidence`, `start_state`, and `end_state`.
   Require the capability's postconditions, not simply a transport success.
   `failed`, `timed_out`, and `interrupted` are terminal non-success outcomes.
7. Use `lab.watch` with the snapshot's cursor to inspect subsequent meaningful
   events, and read a fresh snapshot for handoff. A watch timeout means no event
   arrived in that interval, not that a command succeeded. After movement/combat,
   handoff requires the authorized safe room, survival, and released ownership.
   Agent tests must start and finish in player-configured safe waiting rooms;
   a nearby refuge is sufficient. The earlier experimental `quick_area` field
   proof does not satisfy this requirement. The local
   [safe-refuge implementation](Agent-Test-Safe-Refuge.md) still requires
   coordinated deployment and an explicitly authorized acceptance test before
   routine live testing resumes.

For a controlled combat campaign, do all model planning at the refuge. Use a
native controller to travel, select targets, execute already reviewed routines,
collect evidence, loot, and return at game speed. Interpret and revise the
configuration only after verified safe handoff. Do not leave a character in a
hunting area while waiting for an answer-model round trip.

MCP tools and their exact types are documented in the generated
[SDK declarations](../../mcp/src/sdk-types.generated.ts). For example, after
separate authorization and replacing the placeholder with a verified current ID:

```text
labctl state Testchar
labctl inventory Testchar "test item"
labctl perform Testchar item.audit --item-id CURRENT_NUMERIC_ID --method look --wait
labctl watch Testchar --once
```

`item.audit` may handle the bound item and restore it; it is not merely a database
query. Explicitly select `look` for an inspection-only diagnostic method; other
methods can involve spells. Every step still passes ActionBroker and the
independent Lich checks.

The isolated `lab.execute_code` tool can combine small state/query operations,
but permits only one mutation (perform or exact-operation stop) per execution and has a short execution limit.
Use direct perform/watch for long operations; it is not a general script runner.

## Direct native go2 travel

`travel.go2` exposes existing Lich/go2 routing without starting a combat test.
It is available through the existing capability interface, including MCP
`lab.perform`, not a generic command or script execution tool. For example,
after authorizing this exact character, destination and route scope:

```text
labctl perform Testmage travel.go2 --arg 'destination="1000"' --expected-generation GENERATION --operation-timeout 60
```

Use a numeric map room ID, not a go2 alias, settings command, or room 4 special
selector. Default operation time is 30 seconds; callers may request up to 120.
Native go2 sends are limited to 256 ordinary movement/door/posture/look commands.
Go2 must support `--preserve-scripts`; Lich must support guarded native child
startup and script-start restrictions. Missing support refuses before launch.
One-trip options disable silver retrieval and typeahead, preserving unrelated
scripts and persisted go2 settings. Routes requiring spending, equipment
handling, spells, or nested scripts are not supported by this initial operation.
This trusts installed go2/map code; it is not a Ruby sandbox.
The broker uses a distinct supervised command selector so an older bridge
rejects the request instead of silently launching its legacy unguarded go2 path.
The selector is internal to LAB; it is not a new go2 command for players.

Admission requires fresh same-generation state, known hands, survival, and
released movement/combat/inventory owners. Existing go2 is never adopted or
killed by name. The exact child receives a startup execution guard and uses the
existing broker authority lease; guards do not make network revocation instant.
Ordinary stop, actions-off, expiry or generation loss deny further sends. Only
the exact child is cancelled, with at most two seconds to confirm teardown.
Cancellation is not arrival and cannot undo an already sent movement.

Success requires fresh destination arrival, survival, standing posture,
unchanged hands, attributed go2 completion and released ownership. An already
at-destination request is a verified no-op. If a test has an unresolved refuge
handoff, travel is allowed only to its original refuge in the same session;
the original equipment/owner recovery proof is still required to clear it.
Travel does not override actions-off or gain authority from an earlier failure.

This operation permits explicitly authorized recovery from the field. It does
not relax the requirement for tests to begin/end in player-designated refuges,
nor replace the test controller's own automatic return. Offline tests cover
admission, cancellation and the bridge child seam. Initial live acceptance of
LAB `9345157` passed an already-at-destination no-op and a short town round trip
between two player-designated safe rooms. Both legs verified arrival, original
equipment and released ownership without alerts. This does not verify long or
special routes, live cancellation, combat recovery, or the separate Quick
post-combat return path.

### Controller-result wait regression

A Quick outing exposed a LAB evidence-wait bug: the 30-second maximum for one
state watch was incorrectly used as the entire controller-result deadline. A
watch timeout then failed the operation and revoked its exact launch while
native go2 was still returning, despite time remaining in the approved outing.
Controller verification now repeats bounded watches, carrying its cursor
forward, until matching terminal evidence or the operation's remaining deadline.
This does not extend execution authority or change native go2 routing.

Native controller status is projected into the bounded event contract before it
crosses the bridge. Detailed trial actions remain summarized as shallow result
records, and nested observation payloads are carried as bounded JSON text; the
authoritative native runtime remains local. If SessionHub rejects a terminal
result, the bridge retains that exact run as an unresolved handoff even when the
character returned safely. After verifying the refuge, original equipment, and
released owners, the player can acknowledge it with `;lab recover RUN_ID confirm`.
The original test outcome is never rewritten by recovery.

The virtual-time regression exercises the real broker, operation runner and
evidence adapter: a return after 40 seconds succeeds within a 90-second budget;
missing evidence still fails and revokes at 90 seconds. Additional tests use
the real state watcher to verify bounded waits, unrelated-result rejection,
short/zero deadlines and already-published evidence. These are offline checks.
Player-authorized live acceptance on 2026-09-10 then verified an ordinary-stop
return and a full post-combat seek outing: four game-confirmed kills, return to
the configured refuge, original equipment restored, all controller owners
released, and no alerts. Loot cleanup remains a separate live acceptance case;
the no-loot combat pass does not establish it.
Follow-up acceptance on 2026-09-11 verified the asynchronous MCP ticket/watch
flow and the bounded controller-result projection end to end: one wraith was
killed with the selected trial routine, final loot completed, native travel
returned to room 26109, the original staff was restored, all owners were
released, and SessionHub accepted the terminal result without alerts.

## Trusted script-test pilot

The [approved plan](Script-Test-Pilot-Plan.md) limits this slice to one trusted
non-combat suite on an already logged-in, player-selected character: at most
20 fixed cases, 20 seconds of local work, and 3 seconds of cleanup within the
existing operation deadline. There is no model round trip between case steps.
No automatic login, world setup, live fuzzing, or multi-character campaign is
provided. The [synthetic example](../../examples/script-tests/README.md) includes
a normal exit and an intentional exception; running all cases should **not** pass.

Preparation is offline and requires an explicitly supplied scripts directory.
After staging and reviewing the runner pair, probe, and manifest in that directory:

```text
labctl tests prepare /PRIVATE/LICH/scripts/lifecycle-probe.json --scripts-dir /PRIVATE/LICH/scripts --character Testmage --room-id 123
```

The output is one controller entry, not a complete manifest. Review it before
adding it to the local `controllers` array in `lab-controllers.json`, preserving
existing entries. LAB and the bridge must load the same registration. Restart
only after the player has stopped LAB safely; preparation never does this for
you. The default distribution still has no registered controllers.

Discover `controller.test-lifecycle-probe`, read a fresh snapshot, and use the
advertised revision and case IDs. The following placeholders must come from
that discovery, not from remembered session data:

```text
labctl perform Testmage controller.test-lifecycle-probe --expected-generation GENERATION --arg 'revision="MANIFEST_SHA256"' --arg 'case_id="CASE_ID"'
labctl stop Testmage --operation-id OPERATION_ID --expected-generation GENERATION
```

Perform without `--wait` returns the operation ID immediately; `--wait` instead
streams progress to a terminal result. MCP returns the same ticket through
`lab.perform`; use `lab.operation_watch` for bounded progress polling and
`lab.stop` for exact cancellation. Watch the operation's terminal evidence after
requesting stop: stop acceptance is not cleanup proof.
Do not retry an ambiguous launch. Tests also stop new steps when local control
is revoked, the session changes, or control cannot be verified.

The private report includes the pinned file digests, not just the manifest hash,
so a changed target with an unchanged suite description remains identifiable.
The runner reports per-case parameters, before/after observations, assertion
expected/actual values, normal exit versus exception, and cleanup. Missing state
is inconclusive; a clean exit alone is not a pass. Remaining cases are skipped
after the first non-pass. Detailed reports are created exclusively with owner-only
permissions beneath the Lich data directory's `lab-script-tests` folder; the
controller result carries the private report locator and compact summary. Keep
these reports out of git. A stuck child's incomplete cleanup blocks subsequent
local test runs until the operator resolves it.

This trusts the target Ruby code; it does not sandbox it. Review the
[trust boundary](Safety.md#protected-operations) before adapting any real script.
Only isolated verification is sufficient for development—not for claiming a
live pass. A live test requires separate exact player authorization.

## Diagnosing conversation and latency

### Direct questions and private question corpora

After the player logs in and authorizes testing that character, the shell can
use the same authenticated `/v1/ask` path as in-game conversation:

```text
labctl ask Testmage "What evidence do you have about my training?"
labctl questions Testmage /PRIVATE/PATH/questions.json --output /PRIVATE/PATH/results.json
```

Both commands default to server-enforced read-only questions, even if global
actions are enabled. State, recorded character data, inventory records, and
configured wiki retrieval remain available; INFO/SKILLS refresh does not.
For separately authorized tests of that existing recon path, add `--allow-recon`.
This does not enable actions or auto-approval, bypass ownership, or add arbitrary
commands. It only permits the normal evidence loop to request fixed INFO/SKILLS
through the existing gates.

The CLI checks service compatibility and a fresh snapshot for the selected
character. Questions bind to the observed session generation at server admission;
a corpus keeps that same generation and stops on a session change or failure.
It never logs in a character, retries ambiguous failures, or switches accounts.

A corpus is a JSON object with one `cases` list, containing 1–20 unique case IDs
and questions (maximum file size 64 KiB). For example, this synthetic corpus can
be run separately against player-selected characters of different classes:

```json
{"cases":[
  {"id":"training-evidence","question":"What evidence do you have about my training, and how current is it?"},
  {"id":"follow-up","question":"Which of those observations would need refreshing?"}
]}
```

Cases run sequentially and use normal temporary dialogue, including any existing
conversation. No implicit forget/reset occurs. For a clean conversation, explicitly
use `;lab forget` before starting; deliberately ordered follow-up cases can then
exercise conversation continuity. Do not ask competing in-game questions during
a test run. Each question may invoke the configured model and incur its normal
cost, and references may use configured network fallbacks.

Results include the question, answer, supplied sources, source diagnostics, elapsed
time, and failures. Corpus output is created exclusively with owner-only file
permissions; an existing file is never overwritten. Keep corpus files and results
in private storage outside the checkout: answers may contain character details.
A returned answer is **not** a correctness pass. Review its factual accuracy,
provenance, uncertainty, and completeness; this runner is not a semantic grader.

### Conversation diagnostics

Conversation can request only advertised evidence tools; fixed character recon
still passes the independent action and approval gates. It cannot supply
arbitrary commands or launch scripts. `;lab sources` identifies evidence
actually supplied to the last answer; source diagnostics distinguish success,
missing data, disabled providers, and fallback decisions. `;lab context` reports
temporary dialogue availability; `;lab forget` clears it and invalidates older
in-flight answers without deleting durable knowledge.

Use `labctl timings` to compare context/retrieval, model, and end-to-end latency.
Model time includes startup, transport, and inference; do not claim one of those
is the bottleneck without a separate measurement. Questions have one active
slot per character, four globally, and the selected profile's deadline. Busy or
timeout is an explicit error, not permission to enqueue endless retries.

## Adding a supported test operation

The [exact-operation controller controls](Controller-Controls.md) document
authenticated HTTP/CLI admission for typed controls of a registered active
controller. The bridge binds opted-in native controllers to the exact child and
runtime; public installation alone does not register a capability. The
[Bigshot and EO Hunter examples](../../examples/controllers/README.md) are
opt-in and require separately authorized live verification.

EO Hunter's bounded trial campaign is intended for questions such as “which of
these three reviewed combat recipes is safest and most resource-efficient for
this creature?” It executes one recipe per selected creature, measures actions,
resources, state changes and elapsed time, then performs native cleanup and
return. It is not an unrestricted command planner, continuous autonomous hunt,
or proof that a result generalizes beyond the observed setup.

If discovery has no suitable operation, propose the smallest capability needed
with its admission checks, exact target binding, evidence, restoration, and
failure tests. A controller must use the existing shared manifest and lifecycle.
Do not bypass the broker with a generic “run any script” capability. That would
change authority and requires a separate design and approval.

Record the tested commit, sanitized setup, capability/arguments, terminal result,
observed evidence, and what was not verified. Keep tokens, account information,
private conversations, and raw session logs out of public reports.
