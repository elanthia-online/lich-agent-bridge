# Loopback protocol

The Lich bridge exchanges bounded JSON with SessionHub, normally at
`http://127.0.0.1:18765`. The separate optional
[frontend-state feed](Frontend-State-Protocol.md) is not this HTTP protocol.

Consult [protocol validation](../../src/lich_agent_bridge/protocol.py),
[HTTP routing](../../src/lich_agent_bridge/server.py), and the generated
[MCP types](../../mcp/src/sdk-types.generated.ts) for exact current schemas.

## State and questions

`POST /v1/state` admits a structured snapshot with explicit character,
game, random bridge generation, increasing sequence, and timezone-aware
observation time. A retired generation cannot become current again.
`POST /v1/event` admits meaningful events for the current generation.

The native player's `;lab recover RUN_ID confirm` command can publish a
`controller_recovery` event after verifying safe recovery. Its data binds
`controller`, `action_id`, `previous_generation`, `room_id`, exact `hands`
(`left`/`right` IDs or null), and literal `operator_confirmed: true`. The event
envelope identifies the current character/generation. LAB consumes this exact
receipt only with independently verified current refuge/equipment/owner state;
it is not a generic lock-reset request or a new MCP capability. See
[player-confirmed recovery](Controller-Controls.md#player-confirmed-recovery-after-a-lab-restart).

`GET /v1/state/CHARACTER` reads the snapshot;
`GET /v1/watch/CHARACTER?cursor=N&timeout=30` waits for bounded events.
Unknown values remain absent rather than being guessed.

`POST /v1/observe` accepts bounded textual observations.
`POST /v1/ask` requests a private answer. Questions can request only the
advertised [evidence tools](Evidence-Gathering-Arc.md); they cannot supply
arbitrary commands. `GET /health` identifies the service and its capabilities
so clients can reject an incompatible listener.

Question payloads require `character` and `question`. Optional `read_only: true`
disables game-command recon for this request server-side, without changing global
action controls. It still permits state/record/reference reads and returns
`capability: "read_only"`. Omitting the flag preserves normal in-game behavior;
`false` does not bypass existing action gates. Only JSON booleans are accepted.
Optional `expected_generation` must be a nonblank generation string and is checked
under the question-admission lock. An unknown/replaced generation fails with
HTTP 409 `question_invalidated` before inference. Session replacement during a
question still invalidates it through the existing cancellation fence.

## SessionHub interface

Authenticated JSON POST routes provide the shared CLI/MCP facade:

- `/v1/session/snapshot`
- `/v1/session/watch`
- `/v1/session/capabilities`
- `/v1/session/inventory/find`
- `/v1/session/wiki/search`
- `/v1/session/alerts`
- `/v1/session/sources`
- `/v1/session/context`
- `/v1/session/forget`
- `/v1/session/perform`
- `/v1/session/operation/watch`
- `/v1/session/operation/stop`
- `/v1/session/operation/control` (HTTP/CLI only; no MCP control tool yet)

Capability discovery returns registered names and strict schemas. The bundled
controller manifest is empty; a client must not assume personal controller or
combat-profile availability.

Perform admits an operation and returns its stable ID. Watch that operation to
`succeeded`, `failed`, `timed_out`, or `interrupted`; avoid submitting
a duplicate merely because the caller lost its connection.
Perform also accepts `expected_generation`; it is mandatory for registered
script-test suites. Stop accepts `character`, `operation_id`, and
`expected_generation` to target one exact run. Script tests require all three;
the legacy character-only stop remains available for non-test operations. A
successful stop request records intent, not verified child cleanup. For dispatched
test launches, the existing action-status response exposes `stop_requested`
without rewriting the launch as an unsent cancellation.

Control requires exactly `character`, `operation_id`, `expected_generation`, and
`control` (`status`, `hold`, `resume`, or `retreat`). It attaches an independently
broker-gated command to the existing operation, not another competing operation.
HTTP 202 means admission only; the response includes the broker action and
`applied: null`. Missing/wrong authentication returns 401; malformed, unknown,
stale, expired, or conflicting requests return 400. The optional local Quick
binding pins the exact native child/runtime; no controller is enabled by default.
See [controller-control limitations](Controller-Controls.md).

Sources reports supplied references and diagnostics, not raw prompts.
Forget clears temporary dialogue and invalidates owned in-flight work without
deleting durable inventory or character knowledge.

## Actions and observations

Authenticated action routes separate proposal, confirmation, dispatch, result,
and control. Credentials stay in private local token files and are never tool
arguments. Actions are bound to character, generation, room, and expiry.

A proposal contains either one allowlisted command or one fully disclosed
bounded utility sequence. Approval applies only to that exact proposal.
Automatic approval remains an independently revocable local delegation.
The bridge revalidates every instruction before execution.

Action status and completion evidence are distinct. Legacy completion can mean
only sent-but-unverified; a registered capability requires its own verified
postconditions. Generation replacement and actions-off invalidate pending work.
Exact operation cancellation does not disable unrelated work.

INFO/SKILLS records include source, observation timestamp, completeness, values,
and skills' observed level where applicable. Recon requires an advancing
same-generation snapshot containing fresh matching records; cached values or
a successful send do not establish completion.

## Failure behavior

Invalid arguments, stale generation, wrong character, changed room, expired
approval, missing evidence, and conflicting ownership fail closed.
Questions share one deadline across retrieval, inference, and evidence work.
Cancellation suppresses late answers and revokes pending owned recon; commands
already sent cannot be recalled.

Clients should use typed interfaces and surface errors truthfully rather than
guessing success from silence. See [Safety](Safety.md).
