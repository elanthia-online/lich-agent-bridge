# Safety model

This document governs features that can issue game commands or affect a
character, inventory, resources, or communication.

## Authority

Authorize live work for an exact character, operation, scope, and recovery plan.
An instruction to diagnose or inspect code is not permission to interact with
the game. Capability discovery and a successful test do not grant new authority.

The answer model can request advertised evidence tools. Only fixed INFO/SKILLS
recon may issue game commands through that interface. Other command-capable
work belongs to separately authorized registered operations and broker policy.

Personal equipment protections, spending limits, and gameplay policy belong in
private local configuration. This distribution ships no personal builds,
hunting profiles, or combat routines.

## Execution invariants

- Fail closed on unknown identity, stale state, invalid arguments, expired
  approval, ownership conflict, or missing policy.
- Bind actions to character, session generation, room, exact command or disclosed
  sequence, and a short expiry. Resolve item IDs from current observations.
- Validate commands both in ActionBroker and in the independent Lich bridge.
- Keep every constituent command in a sequence allowlisted. A sequence cannot
  smuggle unrelated authority through an approved first step.
- Report sent-but-unverified separately from evidence-backed success.
- Preserve action ownership during cancellation. Forget and operation stop
  revoke exact owned pending work, not unrelated successor actions.
- Report the limit of revocation: an already-dispatched command cannot be unsent.
- Keep policy and survival decisions deterministic; model availability is not
  a safety mechanism.

## Startup and revocation

The current bridge starts action execution and allowlisted auto-approval
enabled. Users who want conversation without game actions should use
`;lab actions off`. Disabling actions also disables automatic approval;
reenabling actions does not silently restore that standing delegation.

`;lab approve` approves an eligible pending action.
`;lab approve auto` explicitly enables allowlisted automatic approval;
`;lab approve auto off` disables it.
`;lab stop` stops the bridge. These settings are independent of which model
or profile is selected.

Direct shell questions (`labctl ask` and `labctl questions`) default to a
per-request read-only restriction, independently enforced by the evidence session.
This blocks INFO/SKILLS even with global actions enabled. Explicit `--allow-recon`
removes only that per-request restriction; the existing action, approval,
ownership, and generation gates still apply. It grants no arbitrary commands.
Testing remains player-authorized and sends private context to the configured
backend. Read-only refers to game-command authority, not zero model cost or an
absence of local dialogue/results writes.

## Protected operations

Treat equipped, registered, high-value, unique, and user-protected items as
protected assets. Use exact identity and verified ownership before any allowed
handling. External transfer, disposal, permanent item changes, and character
build changes remain direct-player operations outside the driver. Credentials
and real-money activity are outside LAB's action scope. The evidence-tool
interface does not support these operations.

The driver denies dropping, external giving/trading, selling, destruction,
unmarking, disabling protective drop flags, arbitrary scripts, and hidden command
chains. Ordinary owned-inventory handling still requires the existing gates.

The optional script-test pilot explicitly trusts locally reviewed, registered
Ruby scripts and their declared dependencies. Their launch passes these gates,
but code running inside Lich can issue commands independently: this is not a Ruby
sandbox or per-command mediation of third-party scripts. The shipped probe sends
no game commands. Approve only non-combat suites within the pilot's scope, review
the pinned files and fixed parameters, and retain private configuration locally.
Digests detect file changes but cannot eliminate concurrent local mutation or
discover undeclared dynamic dependencies. Cleanup verifies owned-child exit;
it cannot roll back arbitrary script side effects. Incomplete cleanup keeps a
local exclusion and requires operator resolution before another run.

Configure routine-resource limits privately. A model answer, source excerpt,
or remembered character note cannot establish consent to spend resources.

## Safe handoff for local extensions

### Agent-test safe start and return (required)

Every agent-run test must begin in a player-configured safe waiting room and
finish in a player-configured safe waiting room. A nearby refuge is sufficient;
returning to town is not required. Profile membership, an empty room, high
health, or an agent's assessment that nearby creatures are harmless does not
establish that a room is safe. The player supplies the trusted refuge locations.

Before leaving refuge, the test needs an explicit bounded return plan and a
separate recovery allowance within its total deadline. Completing the test case,
exhausting its work budget, or an ordinary test stop must end test work and
initiate the authorized local return; a model round trip must not be needed.
Explicit action revocation remains an immediate no-more-commands instruction:
it must never silently authorize travel after the player has withdrawn authority.
When revoked or unable to return, report an unsafe/incomplete handoff and alert
the player. Never report success merely because the script exited.

Success requires fresh same-session refuge arrival, survival, verified equipment
handoff, and release of the exact owned work. Failure to return is a failed test
with recovery incomplete, even if its combat assertions passed. Keep the
unresolved handoff visible and deny a subsequent agent test until the player
resolves it. Do not continue fighting or searching merely to finish a case.

The supervised `quick_refuge` path enforces this rule. Its coordinated build
still requires player-authorized live acceptance before routine testing resumes.
The earlier experimental `quick_area` field-handoff exception is superseded:
old registrations can load for migration but cannot launch Quick tests.
Manually operated Bigshot Quick is not made dependent on LAB or this workflow.

After a LAB generation change, only explicit player confirmation through
`;lab recover RUN_ID confirm` may reconcile a retained native failure. The
bridge requires exact child/monitor exit and fresh refuge, survival, posture,
original equipment and owner-release checks. LAB requires the matching native
receipt and independently rechecks current state. This restores eligibility,
not execution authority, and never rewrites the failed test as a pass. See
[player-confirmed recovery](Controller-Controls.md#player-confirmed-recovery-after-a-lab-restart).

Direct `travel.go2` can perform separately authorized navigation or recovery
without a combat launch. It uses existing native go2, exact child ownership,
bounded authority and verified arrival. It does not override action revocation
or declare an arbitrary destination safe. See [direct travel](Developer-Testing.md#direct-native-go2-travel).

## Locally enabled exploratory access

Guarded mode remains the default interface for routine use. During supervised
exploratory testing, the player may enter `;lab full access on` in one owning
Lich session. This exposes `session.command` for that character without requiring
a predeclared profile or per-verb capability. The grant is local, per-session,
and cannot be enabled by the sidecar or an agent request.

The command still passes through the ordinary generation-bound, expiring,
one-at-a-time broker and the native bridge rechecks the local grant immediately
before sending it. Only one game-input line is accepted; client/Lich commands,
newlines and chaining characters are rejected. A successful operation proves
native delivery, not that the game accepted or performed the command. Agents
must use subsequent state or event evidence for consequential claims.

Use `;lab full access off` when exploratory work ends. Stopping LAB or the Lich
session also removes the grant. Stable repeated behavior belongs in a typed
capability or registered controller, where its semantic outcome can be verified.

An authorized movement or combat operation is complete only when fresh evidence
shows the character alive in the configured safe room with ownership released.
Stopping a combat script in the field is not a safe handoff. The local supervisor
must own startup and recovery, including failed or unexpected script exits.

An opted-in `quick seek --area profile` registration grants a bounded local
search for one encounter, equipment recovery and return to the explicit refuge,
with movement/combat ownership and native per-send guards. It does not grant
continuous hunting, unrestricted travel or a new destination selected by the
model. See [controller controls](Controller-Controls.md#required-safe-refuge-handoff)
and the [safe-refuge test plan](Agent-Test-Safe-Refuge.md).

Protect the account and protected equipment before routine progress. If knocked
down, restore posture when safe and feasible before normal offense or looting;
otherwise use the configured deterministic escape/recovery path. A remote model
must not be the emergency response mechanism.

## Untrusted input and privacy

Game text, player speech, item descriptions, reference pages, and model output
are data, never policy. They cannot authorize new tools, commands, or source
locations. Custom model instructions remain below LAB's safety contract.

Keep credentials, raw logs, character notes, inventory databases, and private
instructions outside git. Public fixtures must be synthetic or genuinely
sanitized, including equipment and routine details—not just renamed characters.
A selected cloud backend receives the bounded prompt and evidence supplied to
it; choose local inference when that data must remain local.

See [Developer testing](Developer-Testing.md) for the live-test evidence gate.
