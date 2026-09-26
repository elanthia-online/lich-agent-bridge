# Jev shadow decisions and compact observations

LAB exposes an optional, disabled-by-default System One decision evaluator via
`lab-decisions` and `DecisionToolbox`. It classifies a bounded observation into
advertised symbolic choices. It cannot submit commands, start scripts, call the
action broker, or enable live execution. Every outcome has `executed: false`.
This is not a replacement for LAB's existing question/research pipeline.

## Why this interface

Fast classification benefits from a precise question, the current facts needed
to answer it, and explicit outcomes of recent attempts. Sending a command is not
the same as achieving its intended effect. Repeatedly polling an operation is
not the same as making several failed attempts.

The RS-SDK investigation informed these distinctions and the decision to keep
compact inputs separate from full observations. This implementation does not
copy its game-specific code, execute model-written code, or add its navigation
logic to LAB. All execution authority remains in LAB's existing broker and
capability runner.

## What is implemented

- `PlayerFrame` carries one plan step, a snapshot, visible target IDs, party
  observations, cooldowns, and optional `decision_context`.
- `IncidentRecord` carries a versioned supervisor incident. Evaluation requires
  the caller to have established `safe_hold`; the evaluator does not establish it.
- `DecisionToolbox.evaluate` requires a current-fence supplier. Generation,
  sequence, room, target IDs, plan identity/revision and incident version must
  match before and after provider evaluation. Player observation age must be
  nonnegative and no greater than the configured decision TTL, also checked
  before and after inference. A newer observation rejects the old answer.
- `evaluate_replay` uses recorded fences and bypasses player wall-clock age for
  historical fixtures. It still checks invocation TTL and incident expiry, and
  explicitly marks audit records `observation_mode: replay`.
- Live posture is rejected. No autonomous loop or runtime adapter is installed.

The default `compact` request asks only for the next symbolic action and, if
there are multiple targets, target priority. Supervisor requests ask only for
the incident response. Unrelated player diagnostic questions, room prose,
opaque recent text and legacy `last_op` blobs are excluded. Structured self,
target, party and cooldown facts remain. Supervisor `facts` remain intact.
This is a bounded projection, not an inference that excluded text is irrelevant
in every situation; callers must promote decision-relevant observations into
structured state or context.

Unknown death, roundtime, readiness and progress are not converted to false.
An empty party list does not establish readiness. Missing action requirements
do not grant permission. Requirements guide classification, not authorization:
the caller still owns all deterministic eligibility and safety checks.

## Caller-authored context

Both player frames and incident observations accept this optional object:

```json
{
  "decision_context": {
    "policy": {"minimum_health_percent": 70, "recovery_active": false},
    "party_complete": false,
    "action_requirements": {
      "attack_target": "Attack only an unclaimed visible target when the configured readiness checks pass; otherwise wait.",
      "wait": "Wait while recovery is active or required readiness facts are unknown."
    },
    "history": [{
      "operation_id": "synthetic-travel-1",
      "generation": "synthetic-generation",
      "plan_id": "synthetic-plan",
      "plan_revision": 1,
      "sequence": 20,
      "kind": "travel",
      "status": "succeeded",
      "effect": "command_sent",
      "objective_progress": null
    }]
  }
}
```

The policy is supplied by the trusted caller, not invented by Jev. Requirements
must name advertised choices. Game text stays in state, never in requirements.
Context is limited to 8 KiB, eight history entries, and 600 characters per
requirement. Unknown fields are rejected. History entries require every shown
field. `effect` is `unknown`, `command_sent`, `observed`, or `failed`.

`objective_progress` is true/false only when an objective-specific measurement
was observed; otherwise it is null. A failed command does not establish that
the world made no progress. `status: succeeded` alone proves neither an observed
game effect nor progress. For example, `session.command` verifies delivery, not
the effect; a travel objective needs an arrival/state observation. The future
controller adapter must provide these measurements. This PR does not infer
them from raw operation records, names, text, or status codes.

History is filtered by generation, plan and revision, and cannot be newer than
the current snapshot. Duplicate operation IDs retain the newest observation;
conflicting summaries at the same sequence fail validation. The request exposes
the filtered history, ignored-entry count, latest measured progress, and a
consecutive-observed-no-progress count. This count is diagnostic, not an automatic
retry cutoff; unknown progress interrupts the count. Callers should scope history
to the objective they are asking about and bound their own retries.

## Budgets and audit

Compact serialized requests must fit 16 KiB; baseline requests fit 64 KiB.
Oversize or invalid requests raise `ValidationError` before a provider call.
No target or safety fact is silently truncated to make them fit. Callers should
handle this as a need to reshape the observation or request review, not permission
to continue. Invalid requests are not provider outcomes and are not audit-appended.

Outcome audits contain request format, canonical payload SHA-256, serialized
byte count, latency, observation mode, fence, validated answer and provider usage.
The digest refers to the projected request actually passed to the provider;
bytes are a payload measure, not a token estimate. Full request bodies and
credentials are not logged. Audit files are private (0600), bounded per record,
and reject symlink targets. Retention/rotation is an operator responsibility.

Provider transport rejects redirects, bounds response size and validates answer
types, choice IDs and probabilities. HTTPS is required except for loopback HTTP
test endpoints. Credentials are read from a named environment variable, never
stored as values in settings. No additional dependencies are required.

## Configuration and offline checks

The optional `[decisions]` configuration defaults to disabled. Example:

```toml
[decisions]
enabled = false
kind = "system_one"
base_url = "https://api.typesafe.ai"
endpoint_path = "/v1/systemone"
credential_env = "JEV_API_KEY"
model = "jev-1.13.0"
timeout_seconds = 2.0
player_interval_seconds = 1.0
decision_ttl_seconds = 1.0
```

The interval is reserved caller configuration; this PR installs no scheduler.
The default audit path is `decisions-shadow.jsonl` in the configured state
directory. Use private configuration and storage for credentials and audits.
Provider URL, model availability, accuracy and latency have not been validated
by live calls in this change.

```sh
lab-decisions --config /path/to/private/config.toml doctor
python -W error::ResourceWarning -m unittest tests.test_decisions tests.test_decision_context tests.test_settings -v
```

`doctor` makes no provider call. Tests use synthetic observations and fake
providers. They exercise payloads through the evaluator, not a benchmark-only
formatter, and cover unknown state, duplicate attempts, stale history, target
loss, payload limits, policy scoping and auditing.

## Later authorized comparison

Only after explicitly enabling the provider and authorizing paid requests, run
both formats with identical fixtures, expectations, model and repeat counts:

```sh
lab-decisions --config /path/to/private/config.toml --request-format compact provider-acceptance player examples/system-one-acceptance-player.example.jsonl examples/system-one-acceptance-player.expectations.json --warmups 1 --repeats 3
lab-decisions --config /path/to/private/config.toml --request-format baseline provider-acceptance player examples/system-one-acceptance-player.example.jsonl examples/system-one-acceptance-player.expectations.json --warmups 1 --repeats 3
```

Use the corresponding supervisor fixture and expectations for incident tests.
The bundled examples only check wiring; they do not represent sufficient
behavioral coverage. Add reviewed cases for contested targets, incomplete party
state, repeated travel/combat stalls, recovery already active and disconnects
before drawing accuracy conclusions. Expired supervisor fixtures must receive
consistent synthetic observation/expiry timestamps before a later replay.

Reports include request bytes, recommendation/target accuracy, high-confidence
wrong answers, rejection/error counts and latency. Smaller inputs alone are not
evidence of better decisions. No improved accuracy, live safety or cost reduction
is claimed by this PR's offline checks.
