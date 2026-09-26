# Agent tests: safe start and return

Status: single-outing implementation, offline regressions, contribution-branch
review and player-authorized live verification pass. The earlier field-handoff
exception is no longer an executable Quick test path.

## Delivery slices

1. Fix the exact owned eLoot definitions-loader ownership interaction.
2. Implement and test one complete bounded refuge-to-area-to-refuge outing.
   Existing field-only registrations must not remain an executable bypass.
3. Build reviewed case batches on that verified outing, retaining each case's
   profile/sequence, equipment observations, creature identity, attributed events,
   limits and independent recovery result. Stop the batch on unsafe or uncertain
   handoff. Use explicit player-reviewed equipment/configuration choices; do not
   infer item properties or grant arbitrary equip/script commands.

The immediate implementation slice is one outing, not a new general-purpose
experiment language or automatic hunting-profile optimizer. A later matrix can
repeat it for different weapons and creature cases. Functional assertions and
statistical measurements must be labelled separately; a small sample is not a
reliable estimate of a rare effect's frequency.

## Scope

Agent-run tests begin and end in explicitly configured safe waiting rooms.
Nearby refuges are valid; town is not required. Reuse registered controller
authority, native routing/retreat and existing equipment recovery. No new combat
engine, model-driven emergency decisions, town service loop or arbitrary travel.
Manually invoked Bigshot Quick behavior remains unchanged.

## Required behavior

1. Admission requires a fresh same-session observation in a configured refuge,
   a bounded test area, an authorized outbound/return plan, and enough remaining
   operation time for test work plus equipment/return cleanup. No configuration,
   no start. An empty hunting room is not automatically a refuge.
2. The local controller owns the outing. It executes the authorized movement
   and bounded test without remote-model decisions between combat events.
3. Case completion, work-budget exhaustion and ordinary test stop transition
   into return, not field handoff. Stop additional search/attacks; settle known
   equipment state using the existing guarded recovery contract, then use the
   configured local refuge adapter. Unknown equipment state is not permission
   to repeat a potentially already-sent command.
4. Keep cleanup permission narrow and bounded. It cannot restart combat, loot
   another room, sell items or extend the operation indefinitely. Explicit
   actions-off/revocation still prevents further commands; report when that
   prevents safe return. Do not redefine the emergency kill switch as travel.
5. Confirm refuge arrival from fresh native state, verify survival and equipment
   handoff, and join the exact owned scripts before final success. Track combat
   assertion outcome separately from recovery outcome. A failed assertion can
   return safely; a passed assertion cannot make a failed return successful.
6. If return is blocked, times out or cannot be verified, publish an explicit
   unsafe/incomplete handoff with last known room and reason, alert the player,
   and retain an unresolved-test admission block until operator resolution.
   Do not claim invulnerability or guaranteed recovery from the game environment.

## Bounded implementation sequence

- Finish the current startup-barrier and compatibility fixes first. They must
  not be folded into an unrelated new lifecycle abstraction.
- Replace successful `quick_area` agent-test handoff with explicit refuge
  admission and terminal proof on both the service and independent bridge.
  Existing experimental registrations must fail with actionable guidance, not
  silently acquire new travel authority or prevent the whole service starting.
- Reuse Bigshot's local area/retreat support for the bounded outing. Account for
  outbound travel, test work, equipment restoration and return separately under
  a finite total deadline. Choose actual limits from the approved route rather
  than assuming the current 30-second test deadline can fit every outing.
- Preserve the existing noncombat room-bound test pilot; require its configured
  room to be a player-designated refuge. No outbound movement is necessary for
  tests which never leave that refuge.
- Document the user-facing ordinary stop versus emergency authority-revocation
  behavior, migration instructions, and exact success/failure evidence.

## Verification gate

Offline: reject a hunting-room start, missing refuge/return policy, stale identity,
and obsolete field-only registration. Exercise successful nearby return, failed
case with successful return, work expiry, ordinary stop during combat/loot,
blocked return, hard deadline, explicit revocation, equipment uncertainty,
session change, and refusal of a new test with unresolved unsafe handoff.
Verify standalone Quick and room-bound noncombat tests retain their contracts.

Live, only with separate player approval: configure a known nearby refuge and
short route, begin there, run one bounded case, and verify actual return and
equipment/owner handoff. Repeat ordinary stop without manufacturing a dangerous
swarm. Fault injection belongs offline. Record what was observed without private
character profiles or raw logs.

The player will need to identify the actual safe waiting room(s) for each test
area; the agent must not guess them from area boundaries or current occupants.

## Implemented interface and remaining work

Register `quick_refuge` with one explicit room ID and 10–120 `return_seconds`,
on both LAB and the bridge. Start the outing with an observed session generation
and an explicit total budget: CLI `--operation-timeout 90`, or MCP/HTTP
`timeout_seconds: 90`. The default remains 30 seconds; only refuge outings may
extend execution up to 300 seconds. CLI `--timeout` still controls watch polling,
not execution. Existing noncombat pilot limits and recon's 20-second cap remain.

The native runtime checks outbound/return reachability through Lich's existing
map graph and delegates transit to Bigshot's existing go2 helper. Refuge and
intermediate travel rooms may be outside the hunting area; area boundaries
still govern seeking, combat and looting. Transit uses an exact owned go2 child
with a native startup execution guard, a 256-send cap and its phase deadline.
It does not invoke the full rest routine or implement a second pathfinder.
This trusts reviewed go2/map code, not arbitrary sandboxed Ruby; unsupported
commands fail closed. Route admission and transit opt into the native
`allow_script_starts: false` restriction, so recursive/detached go2 launches
are rejected before startup instead of escaping the guard. One-trip
`--preserve-scripts` leaves display helpers running. Ordinary go2 is unchanged.
Work, equipment recovery and return have
fixed deadlines inside the original operation budget. Ordinary stop requests
return; actions-off and expired/replaced authority prohibit further sends.

The result retains independent work and recovery outcomes, original hand IDs,
phase travel counts/reasons and one attributed transcript. Return failure leaves
an exclusion on both sides; same-session fresh refuge, original equipment and
released ownership can resolve it. A changed session cannot silently clear the
old unresolved run. This is not a guarantee against game hazards or disconnects.

Player-authorized live acceptance on 2026-09-10 began in a configured refuge
with known equipment and free ownership. A bounded seek outing traversed 28
rooms, issued four attributed attacks which the game transcript confirmed as
four kills, and returned 28 rooms to the same refuge. Fresh terminal state
verified the original held-item identity, full health, released movement,
combat, and inventory ownership, and no alerts. A separate ordinary-stop run
returned to the same refuge with equipment and ownership restored. These
observations validate the one-outing lifecycle, not every route, creature,
profile, or game hazard.

A rejected native preflight before child creation now emits an exact-run
`controlled_start_rejected` receipt. LAB clears only that new operation's
handoff latch after independently verifying the same generation, refuge,
equipment, and released owners; missing or ambiguous proof remains fail-closed.
An older unresolved native run survived a bridge reload during acceptance and
correctly required explicit `;lab recover RUN_ID confirm` acknowledgment.

Remaining product work is the subsequent reviewed case-batch layer using this
verified outing. Automated weapon swapping, generic test scripting,
multi-character campaigns, and parameter optimization are not implemented by
this slice.
