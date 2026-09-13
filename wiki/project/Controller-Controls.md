# Exact-operation controller controls

Status: authenticated HTTP/CLI control admission and exact-child native runtime
binding are implemented with offline tests. Bigshot Quick remains supported,
and the generic `controller_refuge` contract admits EO Hunter's bounded trial
runtime. No controller is registered by the public distribution. This work does
not deploy scripts or establish live compatibility. The
[controller examples](../../examples/controllers/README.md) require explicit
local review and registration.

## Implemented interface

The matching Python/Ruby controller schemas accept actions of `kind: control`,
named `status`, `hold`, `resume`, or `retreat`. Each takes exactly one `run_id`
parameter of type `action_id`: 16 lowercase hexadecimal digits identifying the
original launch's broker action. Controls cannot contain script arguments or a
launch mode, become the controller's launch capability, or enter a command batch.
Mutating controls retain combat classification and confirmation policy. Status is
inspection. Immediate script kill is not a typed control or verified extraction.

Controllers opting in must declare `control_owner_scripts`, including their
native script and drawn from the existing `owner_scripts` exclusion list.
An exclusion is not permission to control a competing owner. The shipped registry
remains empty; synthetic fixtures are not active registrations.

Declared controller lanes also participate in observed ownership. In particular,
an inventory-capable controller must be recognized as the inventory owner while
its exact runtime executes cleanup. Independent eLoot, healing, and inventory
scripts take precedence as conflicts; an unrecognized owner is not silently
treated as permission to continue. LAB Inventory reports an automatic
passive/active flag: passive observation does not reserve the inventory lane,
so it can remain running across logins and controller tests. Explicit enhancive
and charge refreshes claim that lane until completion (including error cleanup)
and use native execution guards to refuse competing movement, combat, or
inventory owners before each send. An older tracker without this interface, or
an unreadable flag, remains a conflict. No persistent user toggle is needed.
On older Lich without native guards, explicit refreshes remain available only
with a validated, explicitly empty controller registry and available bridge
ownership checks. They retain the exclusive lane claim and initial owner check,
not per-send revocation. Registering controllers or unavailable registry state
disables this fallback. Native guards remain mandatory whenever present; see
[setup compatibility](Setup-and-Operations.md#install-lich-dependencies).

The bound Quick may briefly own an exact direct `eloot --load-room-api` native
child to load definitions. Only that child's native identity and complete exact
argument list are exempt from its local ownership conflict check. Ordinary,
foreign, sibling and differently parameterized eLoot remain conflicts; snapshot
visibility is unchanged. This is trusted-script ownership, not a Ruby sandbox.

Supervised refuge transit similarly permits only an exact direct native `go2`
child that the bound Quick owner currently identifies through
`quick_refuge_travel_child?`. Both child-list identity and a literal `true` from
that predicate are required. Foreign or sibling go2 scripts, inactive/stale
travel markers, missing predicates, and unreadable observations remain conflicts.
The child remains visible in snapshots; this is not a name-based movement
exemption or permission to launch arbitrary travel.

`CapabilityRunner.control_controller(operation_id, character=...,
expected_generation=..., control=...)` submits a control through ActionBroker for
an existing active operation. It does not create another operation or relax the
one-operation-per-character rule. It verifies fresh identity and ownership,
retains the original launch token across secondary actions, and bounds pending
controls to 32 with at most five seconds of authority, never beyond the operation
deadline. The response includes the broker action and `applied: null`: admission
is not evidence that the control was applied.

`POST /v1/session/operation/control` uses the existing authenticated server and
requires exactly `character`, `operation_id`, `expected_generation`, and `control`.
It returns HTTP 202 for broker admission, 400 for invalid/stale requests, and 401
for missing/wrong authentication. No additional operation, listener, credential,
or model invocation is created. The matching CLI is:

```text
labctl control CHARACTER hold --operation-id OPERATION_ID --expected-generation GENERATION
```

The same command accepts `status`, `resume`, and `retreat`. It sends once and
prints admission JSON; it does not wait for application or retry ambiguities.
The HTTP/CLI path is tested through the actual local server with a synthetic
active controller. No MCP control tool is added by this slice.

Cancellation revokes exact owned pending controls and marks the original launch.
A dispatched control receives
a `stop_requested` marker through existing action-status observations; it cannot
be unsent. Unrelated actions are not revoked.

For an already dispatched refuge outing, ordinary operation stop instead marks
the launch `return_requested`. The bridge queues one local stop-to-return request
while retaining the launch lease. Pending controls are revoked. Actions-off,
explicit hard revocation, missing authority and generation loss still deny all
further commands, including travel. Neither kind of stop proves safe arrival.

## Optional native controller binding

Generic native controllers publish `controller_runtime` and, after exit,
`controller_result` on their exact Script instance. LAB prefers those methods
and retains compatibility with Bigshot's `quick_combat_runtime` and
`quick_combat_result`. A generic script declares
`SUPERVISED_CONTROLLER_PROTOCOL = 1` and `REFUGE_START_PROTOCOL = 1`; Bigshot's
historical `SUPERVISED_START_PROTOCOL` declaration remains accepted. The
generic refuge child predicate is `controller_refuge_travel_child?`, with the
Bigshot-specific predicate retained as a compatibility fallback.

All remaining identity, lease, control, deadline, restoration, and evidence
requirements in this section apply equally to both interfaces. Method-name
compatibility is not authority: LAB still pins the exact native child, original
action, character, generation, runtime object, and owned go2 child.

The bridge launches opted-in controllers with native `Script.start_child`, pins
its exact returned Script object, original launch, character, and session, then
binds only that instance's fixed `quick_combat_runtime` publication using
`LabControllerControls::Binding`. No arbitrary method names or script-name
rediscovery are accepted. Supervised startup must publish and activate before
any work; a terminal snapshot before activation is a startup failure, not an
alternative successful launch. After activation, fast completion still retains
its exact-instance immutable `quick_combat_result` snapshot.

Before spawning, the bridge resolves the trusted local script using Lich and
checks its literal `SUPERVISED_START_PROTOCOL = 1` and `REFUGE_START_PROTOCOL = 1`
declarations. It then appends the private `--supervised-start-v1 WORK,CLEANUP`
and `--supervised-refuge-v1 ROOM,RETURN_DEADLINE` selectors, never modifying the
registered player command or persisted presets. Historical scripts without the
declaration are refused before launch; older expanded Quick parsers also reject
the unfamiliar selector. This is compatibility checking of trusted local code,
not a Ruby sandbox, source authentication, or protection against concurrent file
replacement. The declaration must not be added to incompatible scripts.

The available predicate checks current local run/session/ownership pins without
issuing commands. Native Quick enforces the approved outing's area/route edges
while its phase is outbound, working, recovering or returning. Each newly queued
control still binds to its current room and is invalid after further movement.
The monitor does not mistake authorized return travel for a room-pin violation.
The separate authority reader uses the existing authenticated action-status
transport on a bridge worker, never on Bigshot's owner thread.

The runtime interface is `request(action, valid: Proc)` plus immutable cached
`status`. Bigshot's owner checks the predicate immediately before applying a
queued control and again before deferred retreat. This requires no LAB import or
global in Bigshot. A rejected runtime request revokes and removes its lease.
The application predicate expires at the control's deadline, not the lifetime
of an already entered escape. Original launch authority governs that lifetime.

The monitor rechecks the exact runtime's terminal state if a lease refresh
fails while the child is exiting. A published command-budget stop remains a
budget failure, not a spurious authority-loss report. This closes admission;
it does not renew authority or allow further commands. A nonterminal run whose
lease fails still receives cooperative stop.

The lease reads cached broker authority for at most 250 milliseconds and obeys
absolute expiry. Observed revocation latches; missing, mismatched, or failed
authority reads deny. Refresh must run off the owner thread. Remote revocation
has polling latency; neither this cache nor control admission retracts an already
applied control or a server command already sent.

Controlled launches carry an internal `controller_deadline` copied from their
owning operation. Public action proposals cannot supply it. Dispatch `expires_at`
is only the admission window; it is not the launched controller's run deadline.
Startup publication wait is capped at three seconds and the operation deadline.
Before spawning, LAB requires at most 300 seconds of remaining operation time
and reserves the configured 10–120 return seconds. If `H` is the monotonic hard
cutoff and `R` the return reserve, work ends at `H-R-12`, equipment-only recovery
ends at `H-R-2`, and return ends at `H-2`. Insufficient work time refuses launch.
Quick installs those immutable windows before publication
and waits without commands for one exact supervisor activation. Only after the
binding, authority lease, and monitor exist does LAB activate the runtime. Its
barrier expires within three seconds or at the work deadline, whichever comes
first; initial GROUP queries run after the barrier and check the same work
deadline and cached authority during every guarded send/wait. Standalone Quick
does not require this private handshake. The existing operation deadline is not extended. Quick
guards enforce the work boundary on sends/waits and between corpse passes;
only its existing bounded equipment recovery may use the ten-second reserve.
Ordinary stop finishes test work and requests return; lost authority still
revokes commands. This
does not guarantee cleanup after arbitrary helper stalls or transport delays,
and does not imply every corpse can be processed within a short operation.
Failure before runtime publication cancels only the exact native child via
`kill(async: true)`, setting its stopping flag synchronously. Compatible Quick
startup/guards reject that flag before commands, including late publication.
This forced startup cancellation is not safe extraction.

Once bound, missing/revoked authority requests cooperative runtime stop, even
during retreat. Ordinary admitted retreat room movement is not itself a hard
revocation. Cleanup is awaited for three seconds (plus in-flight bounded polling);
incomplete cleanup reports failure and retains the exact-child exclusion until
join can be confirmed. No successor is killed or released by script name.

Queued acknowledgements and cached status are not attributed application results.
Terminal success requires fresh survival, configured refuge arrival, restored
hands, standing posture, exact owner join and released lanes, independently of
the work result. The bridge preserves these distinctions and the existing
broker/independent Lich checks, without another listener or authentication path.

<a id="optional-profile-area-field-handoff"></a>

### Required safe-refuge handoff

Agent-controlled Quick requires `safe_handoff: {"kind": "quick_refuge",
"room_id": "1000", "return_seconds": 30}` with a player-reviewed refuge. The
example room is synthetic, not a recommended location. Room IDs must be positive
and cannot be room 4; return seconds must be an integer from 10 through 120.
The registration requires native controlled Bigshot, movement/combat lanes and
fixed `--area profile` in every launch. Duplicate, variable or suffix-supplied
area options and private supervisor flags are rejected.

The character must start alive and standing with known hands in that refuge.
Bigshot resolves the selected profile area and preflights the bounded native
outbound/return routes. LAB neither traverses the map nor invents refuge rooms.
One local controller owns the outing, equipment recovery and return without a
model round trip. A nearby safe waiting room is sufficient; town is not required.

Terminal `runtime.refuge` must contain the configured integer `room_id`,
`phase: "finished"`, `returned: true` and `equipment_restored: true`.
`runtime.work_result` preserves the independent combat-case outcome. A failed
case with successful recovery remains a failed test; a passed case with failed
return is an unsafe handoff. The bridge independently reads fresh same-session
room, survival, posture and original hand identities, verifies exact child join
and released owners, and retains unresolved unsafe-run exclusion. A later
same-session refuge/equipment/owner observation can clear that local exclusion;
relogging is not an automatic reset. Unknown or missing proof cannot release it.

Old `quick_area` and room-bound controlled Quick registrations still load for
migration, but launches and controls are refused. They do not silently gain
travel authority. Native `bigshot` registrations whose resolved arguments begin
with `quick` cannot bypass this rule by omitting controls; the bridge refuses
that generic launch path too. Other registered non-Quick Bigshot launches are
unchanged. Manual standalone Quick and the noncombat room-bound test
pilot retain their separate contracts. The shipped controller registry is empty.

### EO Hunter bounded trial campaigns

EO Hunter's native controller performs a finite sequence of reviewed profile
routines while its ordinary engine continues to own target selection, combat,
movement, looting, flee policy, equipment handling, and return-to-rest logic.
LAB supplies only the selected profile and a manifest-enumerated sequence such
as `a-b-c`; it does not construct spell commands in the field.

Each letter selects one existing profile routine for one creature. EO Hunter
records the creature identity and state changes, each resolved routine action,
resource deltas, elapsed time, and terminal outcome. The sequence is capped at
five creatures, 12 actions and 45 seconds per creature. A target that disappears
without a confirmed death fails the campaign. Completing the sequence requests
native final looting and safe return. Failure also requests safe return while
preserving the failed work result.

This makes a low-danger empirical comparison possible without model latency in
combat: prepare several complete recipes at the refuge, let the runtime execute
and measure them at game speed, then interpret their evidence after it has
returned. Lethality alone is insufficient; evaluation should prioritize safe
and reliable completion, then resource cost and elapsed time. Results apply to
the observed creature, build, and state and do not establish a universal
optimal rotation.

Use `safe_handoff: {"kind": "controller_refuge", ...}` and the synthetic
[EO Hunter registration](../../examples/controllers/eohunter-trial-campaign.json).
The public registry remains empty, and real character profiles remain private.

### Find one encounter without agent round trips

An opt-in registration can launch `bigshot quick seek --area profile --preset NAME`
using a finite, locally reviewed preset enum. See the synthetic
[seek registration](../../examples/controllers/bigshot-quick-seek.json).
The public registry remains empty. Seek requires a compatible native Bigshot
build, `quick_refuge` handoff, and declared movement/combat lanes. Include the
inventory lane and eLoot owner exclusion when the preset permits cleanup.

Bigshot locally searches ordinary mapped exits within its frozen profile area,
at most 12 steps/30 seconds or the preset's smaller action/time limits, then
clears the first eligible encounter and optionally calls eLoot. Each movement
allows one direction send with a three-second arrival limit, not arbitrary
commands or scripted exits. A target already present skips search. Once found,
the combat room is pinned; search never resumes after a clear or departing target.
No full hunting, follower, upkeep, sell/rest cycle, or model-driven per-room loop
is introduced. Existing modes retain their previous movement behavior.

LAB controls and cancellation remain bound to the exact operation/generation and
fresh current room. Native guards enforce the area during each search send;
terminal handoff requires correlated cleanup, survival, restored equipment and
return to the configured refuge. Search usage is published
under runtime `search`; dispatch and clean handoff do not prove combat effects.
The existing operation deadline still applies to the whole request. Offline
tests are not a live pass; deployment and supervised testing remain separate.
Offline tests cover search-to-combat wiring, cancellation before sends,
arrival/target races, denied callbacks/exits and separate recovery evidence.
The safe-refuge revision still requires coordinated live verification with
explicit player authorization; older field-only smoke tests do not establish it.

### Bounded status presentation

Terminal runtime reports and control acknowledgements share the bridge's Quick
status presentation adapter. The existing 32,768-byte status limit and server
event depth/string limits remain unchanged. Small plain-command reports are
unchanged. A native grouped command is rendered as JSON **text**, marked
`command_presentation: "json"`; this is display data, not a new command or replay
format. Command text exceeding 4,000 characters is shortened and marked with
`command_omitted_characters`. Truncated JSON text need not be valid JSON.

If needed to meet the byte limit, only the oldest transcript entries are omitted;
the newest outcome/send/sequence records are retained. `observation_transport`
reports `omitted_entries` and the transmitted `retained_sequence_range`.
Bigshot's original `observations_total`, `observations_dropped`, and
`observation_sequence_range` still describe its own runtime ring, not this
additional transport trimming. Selected configuration names, limits, timings,
usage totals, control fields, and exact result identities are preserved. The
runtime's immutable snapshot is never modified. A summary which exceeds the
limit even without transcript entries is still rejected, not silently rewritten.
These presentation changes do not verify game effects or control application.

Refuge reports retain `work_result` state, reason and usage as a summary on the
wire. Its duplicate transcript is omitted, with
`work_result.observation_transport.observations_source: "runtime.observations"`
and an explicit omitted-entry count. Root observations are the sole transported
transcript and retain their existing bounded newest-entry projection. The
native immutable work result is not changed by serialization.

Verified offline with 718 Python tests and 132 focused Ruby bridge/controller/test-runner
tests (789 assertions, no skips). A cross-check using Bigshot's actual controller
report producer and LAB's event validator covers simple commands, grouped
commands, oversized grouped transcripts, and long command text. This confirms
transport compatibility, not live combat behavior.

## Player-confirmed recovery after a LAB restart

`;lab recover` lists retained native Quick handoff failures for the current
character. After manually restoring the character, the player can use
`;lab recover RUN_ID confirm` with the displayed 16-character launch action ID.
There is no blanket reset and no advertised model capability for confirmation.

The bridge verifies that exact child and monitor have exited, the current
character is alive, unstunned and standing in the run's original refuge, both
original hand IDs match, and movement/combat/inventory owners are released.
It reads native state rather than trusting a prior HTTP snapshot. Only this
explicit acknowledgement may reconcile a retained run from an earlier LAB
generation; automatic recovery still requires the original generation.

The existing authenticated event feed carries `controller_recovery`, binding
the exact controller/action ID, previous generation, current event generation,
refuge and hand identities, with `operator_confirmed: true`. Publication failure
retains the lock. The bridge retains the acknowledged run until superseded so
the same explicit command can republish a lost/expired receipt. LAB requires a
matching receipt plus fresh safe current state before clearing its old-generation
exclusion during the next admission. A receipt by itself is not current safety;
an evicted receipt must be explicitly republished, never guessed.

Recovery sends no game commands, cancels no scripts, renews no action authority,
and does not change the original test result or remove its failure alerts.
It acknowledges restored safety, not successful execution of the failed test.

## Verification boundary

### Paired-trial adapter pilot

`lich/lab-paired-trial.rb` is an opt-in adapter for a separately reviewed native
EOHunter GroupTrial runner. It registers no capabilities by default and is not
an arbitrary-script launcher. A local manifest and reviewed runner are still
required; neither private character profiles nor test destinations ship here.

The adapter reuses EOHunter's `Controller::Guard` and immutable projections.
LAB binds the exact runtime and supplies its existing cached operation lease.
Control callbacks enqueue intent only; the owner calls `poll` outside disposable
combat guards and hands return requests to the native participant. Ordinary
stop/retreat preserves bounded return authority. Revocation or a changed session
does not permit further commands. Hold/resume are not supported by this pilot.

The runner must call `finish` after native work/child cleanup and publish the
returned immutable status as its exact instance's `controller_result`. Successful
combat requires both pinned native receipts and fresh local refuge/equipment
evidence. A lost peer can produce a failed test with verified local recovery;
it cannot produce a successful pair result. Missing final observation produces
an unsafe handoff, not an inferred return. Native policy still owns targeting,
movement, equipment and recovery; the adapter sends no game commands.

Run the adapter tests with `LAB_TEST_HUNTER_ROOT=/path/to/reviewed/hunter ruby
tests/lab_paired_trial_test.rb`. They use Hunter's actual Guard/Immutable plus
LAB's Binding/Lease with synthetic observations. Without that checkout they
explicitly skip. They do not establish live game or paired transport behavior.

### Existing controller coverage

Tests use the production schemas, ActionBroker, CapabilityRunner, Ruby bridge
launch/dispatch/monitor and lease adapter, plus authenticated HTTP/CLI transport,
with synthetic state, authority, child lifecycle, and runtime observations. They cover
malformed controls, exact tokens, competing ownership, stale sessions/rooms,
expiry, cancellation, queue bounds, and queued-versus-applied reporting. They do
not prove live game behavior. Native child ownership is additionally source-backed
by Lich's `Script.start_child` lifecycle tests; native Quick stopping-owner
refusal belongs to Bigshot's startup/execution tests, not the synthetic bridge
fixture. Live verification requires explicit authorization.
