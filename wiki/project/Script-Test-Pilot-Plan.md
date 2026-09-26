# Trusted non-combat script testing pilot

Approved direction: 2026-09-07, following the maintainer's request to write the
plan and implement the bounded first slice using subagents. This plan refines
the existing registered-controller extension seam; it does not replace the
research workspace or reopen deferred passage-selection issue #7.

## Outcome

A developer can prepare one trusted, revision-pinned non-combat script suite,
inspect its grant, run declared parameterized cases through LAB, and obtain
explicit assertion and cleanup results in a private report. An agent uses the
same CLI/MCP operation interface as a developer. No model call occurs between
case steps. The first shipped example is a harmless lifecycle probe, not a
third-party gameplay script or an invented reproduction of a community bug.

## Scope fence

- Reuse registered controllers, ActionBroker, session generations, operation
  watching, and Lich-side validation. Do not create a parallel command executor.
- One active suite on one player-selected, already logged-in character.
- Explicit local registration and file digests; remote requests select approved
  suite/case/revision values, never script paths, Ruby code, or shell commands.
- Keep the existing 30-second operation envelope for this short-case pilot:
  at most 20 declared cases, at most 20 seconds of local test work, and bounded
  cleanup within the remaining envelope. This is not yet a long-campaign runner.
- Preconditions, simple named state assertions, exact script lifecycle results,
  setup observation, owned-child stop/cleanup, private JSON reports, and one
  synthetic non-combat suite. Test setup does not automatically create inventory
  conditions, buy resources, alter equipment, or change character settings.
- No combat, movement, sales, resource consumption, multi-character scheduling,
  automatic login, arbitrary script registration, live fuzzing, automatic issue
  posting, general test DSL, or automated replay-fixture generator in this slice.
- No deployment, service restart, live script test, commit, or push until the
  offline integration results are presented and the player authorizes that step.
- Review may fix violations of this contract, not silently add new capabilities.
  Any expanded authority, longer campaign envelope, extra executor, or new
  framework needs the reason/impact disclosed and agreed first.

## Trust and lifecycle contract

`.lic` code executes inside Lich and can bypass LAB's broker for its own commands.
This pilot does not sandbox Ruby. Registration is explicit trust in reviewed code,
its declared dependencies and configuration, not evidence that it is harmless.
Pinning detects changed files; it cannot eliminate concurrent trusted-local-file
mutation or discover arbitrary dynamic dependencies. Disclose these limits.

Resolve the actual Lich script file before checking its digest; custom-directory
shadowing and prefix fallback must not substitute unapproved bytes. Reject
missing/changed/ambiguous targets and unsupported lifecycle capabilities before
launch. Require the current owning script context, retain exact child instances,
use bounded joins and bounded kill_sync, and distinguish exceptions from normal
completion. Never treat name disappearance alone as successful cleanup. The
inspected lifecycle source is Lich revision
4ed650ba9dab59bb524609986267302e219c651b; compatibility must be checked at runtime,
not inferred from the installed version number alone.

Fresh identity, generation, allowed room, alive/unstunned state, and relevant
owner exclusions are checked at admission and during local work. Snapshot setup
and cleanup assertions make expected restoration explicit; they do not promise
rollback of arbitrary script side effects. Failure or uncertain cleanup stops
remaining cases. If a child cannot finish cleanup, retain a local exclusion and
require operator resolution; do not start a successor on a false clean state.

Stop must target the exact operation/generation. Revoke its pending launch, or
request stop of its exact active run; a delayed stop must not touch a successor.
Actions-off, bridge loss, and generation change stop new steps locally. Report
stop requested separately from verified cleanup. No emergency game command is
added by this pilot.

## Small interface

- Offline preparation validates a bounded declarative suite and emits a reviewed
  controller registration with pinned files. It does not install or enable it.
- Existing capability discovery describes registered suites and approved cases.
- Existing perform/start/watch returns a run ID and attributable progress/result.
- Exact-operation stop is exposed to CLI and MCP through the existing operation
  stop route, with ID/generation fencing rather than an arbitrary command tool.
- Private reports use exclusive creation and owner-only permissions, recording
  suite/revision, run identity, cases/parameters, assertions, before/after state,
  lifecycle result and cleanup outcome. No automatic public export.

Suite descriptions are data: bounded named cases with fixed argument tokens and
state assertions using a small allowlist of fields/operators. No evaluated
expressions, executable assertion strings, arbitrary output paths, or implicit
Cartesian expansion. Successful script exit is necessary but not a substitute
for the declared assertions. Pass, fail, skipped and inconclusive stay distinct.

## Work packages

### Shared pilot contract

Controller registrations may opt in with `test_suite` metadata containing
`manifest` (scripts-root-relative JSON path) and `files` (relative path to SHA-256
map). The map pins the manifest, `lab-test-runner.lic`, `lab-test-runner.rb`, the
target `.lic`, and explicitly declared local dependencies. Ordinary registrations
remain unchanged. Public defaults still register nothing.

Suite JSON fields are `version: 1`, `id`, `script` (exact extensionless name),
`files` (declared dependency paths, including target), `cases`, and `limits`.
Each case has `id`, `args` (fixed bounded tokens), and `assertions`. Assertions
use `field`, `op` (`equals` or `unchanged`), and `value` for equals. Initial fields
are `room_id`, `right_hand_id`, `left_hand_id`, `health`, `mana`, `spirit`, `dead`,
and `stunned`. Missing observation is inconclusive, never equal to null by default.
Every case requires an assertion. Limits are `case_seconds`, `run_seconds`, and
`cleanup_seconds`, with run_seconds <= 20 and cleanup_seconds <= 3.

Offline preparation takes an explicitly supplied scripts directory, manifest,
character, and allowed room. It emits one controller entry named `test-ID`, with
script `lab-test-runner`, globals `$lab_test_result`/`$lab_test_cancel`, and
movement/combat exclusion lanes (ownership exclusion, not gameplay permission).
The run command is `lab-test ID {revision} {case_id}`; revision is an enum with
the manifest digest and case_id is an enum of declared IDs plus `all`. The runner
receives `ID revision case_id`. Registration and pinning are reviewed local setup,
never an automatic consequence of a remote perform request.

Bridge launch supplies action-bound test context; the local runner polls the
existing authenticated action-status interface at a bounded interval for the
exact run's stop request. Backend stop cancels an undispatched launch or records
`stop_requested` for that test launch without pretending dispatched work was
unsent. The record remains available through cleanup. No status response grants
new authority; unavailable control or changed identity stops local work.

The bridge's existing controller result carries a compact report summary and a
private report locator. Detailed assertions and snapshots stay in the owner-only
report. Cleanup-incomplete is terminal non-success for the remote request but
must preserve local exclusion against another run until the child truly exits.

### Ownership

1. Controller: plan, common contract, integration, public/private separation,
   CLI/MCP wiring, documentation, final verification and handoff.
2. Trusted suite preparation: Python schema/preparation module and synthetic
   tests, sample suite/probe. Emit registration without mutating local setup.
3. Backend lifecycle: minimal optional test-controller metadata, exact generation
   admission/stop, pending-launch cancellation, attributable result/cleanup checks.
   Preserve non-test operations and existing controller behavior unless a shared
   fix is directly required and covered by regression tests.
4. Lich local runner: matching trusted metadata validation, exact script resolution
   and digests, owned child execution, assertions, local stop/cleanup and exclusion,
   bounded structured results and private reports; deterministic fake-Lich tests.

Workers have disjoint file ownership and agree shared schemas before edits. The
controller verifies source-backed outcomes, not agent confidence. Sophia tools
are unavailable; coordination and one bounded closeout remain source-backed here.

## Verification and acceptance gates

- Red/green isolated tests cover invalid suites, digest mismatch, path traversal,
  shadowed/prefix scripts, changed parameters, unsupported lifecycle, stale or
  replaced sessions, concurrent runs, delayed stop, revoked pending approval,
  failed assertions, script errors, timeouts, and incomplete cleanup.
- The original suite input drives the actual runner through fake Lich; test
  evidence includes assertion expected/actual values and final restoration.
- CLI/MCP exercise the same registered run/stop interfaces with no hidden game
  authority. Private reports cannot overwrite existing files or expose tokens.
- Python, Ruby, MCP, generated SDK, documentation, and privacy checks pass.
- Then present implemented scope/limits and request an exact player-authorized
  live test of the harmless sample. Record loaded lifecycle support, script hashes,
  actual assertions and cleanup. No live claim from mocked tests.
- Rollback for an eventual deployment: back up changed runtime files, preserve
  settings/databases, disable only the new registration, restore modules with LAB
  stopped, and verify health. No automatic character restoration is promised.

## Current status

## Controller closeout — 2026-09-07

Outcome: the scoped offline pilot is implemented using the existing controller
path: explicit pinned preparation, fixed cases/assertions, exact-run stop,
bounded owned-child cleanup, and private reports including file digests.
No default registration or new gameplay authority was enabled.

Evidence: tests reproduce schema drift, approval/deadline races, stale stops,
failed assertions, missing observations, report-storage failure, and a stuck
child delaying its wrapper. Incomplete cleanup now publishes non-success while
retaining exclusion; wrapper failure cannot turn a child pass into success.
The shipped example passes Python preparation and both language parsers.

Verification: 555 Python tests; 146 tests across the six Ruby harnesses (699
assertions); 24 MCP tests; SDK drift/type/build checks; offline Python wheel;
detached-launch regression; Markdown links and whitespace checks passed.
The final report-digest addition was rechecked in runner/bridge and cross-language
tests. Ruby verification used 4.0.5; hosted CI and other runtime versions were
not run in this work.

At the offline closeout, player-authorized deployment and the harmless probe in
a real Lich session remained outstanding. The follow-up below records those
separately; fake lifecycle tests are not live verification. No expansion beyond
this plan was implemented. Sophia was unavailable during implementation.

### Authorized live smoke tests — 2026-09-07

After a backed-up local deployment and a fresh Lich login, the player separately
authorized one run of each shipped case on one safe, already logged-in character.
Both used the CLI's registered operation path with the freshly observed session
generation and advertised manifest revision. The runner's lifecycle capability
checks admitted execution on the installed Lich runtime; no game commands were
issued by either probe.

- `normal`: operation succeeded. The private report confirmed normal child exit,
  all four declared assertions passing, and complete cleanup.
- `intentional-error`: operation failed as intended. The report captured the
  deliberate `RuntimeError`, distinguished it from normal completion, confirmed
  all three unchanged-state assertions, and recorded complete cleanup. The
  runner did not misreport a clean teardown as a successful test.
- Fresh post-run snapshots confirmed the same session, room, and hands, an alive
  character, no remaining runner/probe scripts, and released movement/combat
  ownership. Reports were owner-only (`0600`) under an owner-only (`0700`)
  directory and included the pinned file digests.

This verifies the success and exception paths only. Timeout, cancellation,
incomplete cleanup, and combined-case early termination remain isolated-test
coverage, not live-tested claims. No third-party gameplay script was tested.
Raw reports, character identifiers, installation paths, and recovery backups
remain private. These checks did not commit, push, or publish the candidate.

### Final review correction

The bounded spec review found a launch-time gap: pin verification could consume
the remaining work budget or overlap a session change after the initial guard.
An already completed child could also be accepted before the deadline check.
Three deterministic regressions reproduced those cases on the prior code. The
runner now rechecks local state/control and the deadline after verification and
before launch, and checks the case deadline before accepting child completion.
This enforces the existing contract without changing test authority or adding
features. It cannot preempt synchronous filesystem or Lich startup calls.

Final local verification: 555 Python tests with the cross-language check enabled;
149 Ruby tests / 714 assertions; 24 MCP tests plus typecheck, generated-SDK drift
check, and build; offline wheel; detached-launch regression; 52 Markdown files
with zero broken local links; and whitespace checks. Checksum-pinned Gitleaks
8.30.1 reported no findings in the candidate or reachable public history. A
separate source/path scan found no private character identifiers, installation
paths, live run identifiers, or personal scripts. Public registrations remain
empty. The standards review found no violations; the spec review's one deadline
finding was corrected and independently rechecked. Hosted CI was not run.

The correction is covered by isolated tests only and was not deployed during
closeout. The live smoke-test observations above apply to the preceding pinned
runner bytes, not the final corrected candidate. A future local deployment must
regenerate the private registration to pin the new helper bytes and reload LAB's
Ruby runtime safely; old grants intentionally do not authorize changed files.
