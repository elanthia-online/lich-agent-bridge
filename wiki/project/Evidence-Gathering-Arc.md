# Bounded model-directed evidence gathering

The model may answer from supplied context or ask LAB for missing observations.
This is an implemented application protocol, not general model tool authority.

## Tools

| Tool | Scope |
| --- | --- |
| `state.read` | Observed room, vitals, effects, wounds, scripts, and hands |
| `character.read` | Recorded/current stats and training; fixed INFO/SKILLS refresh when eligible |
| `inventory.search` | This character's recorded item dossiers |
| `combat.report` | Retained native trial reports, bound to this character and LAB operation; no game commands |
| `knowledge.search` | Compact source discovery; reference, character, or development scope |
| `knowledge.read` | Issued source/section handle and optional continuation; no arbitrary paths or URLs |

Character, generation, provider configuration, and deadlines are application
bound. Model arguments cannot replace them. Every request batch is validated
before any member runs.

## Budget

A direct answer uses one model call. Evidence gathering allows at most three
rounds, four requests per batch, and eight total requests under one existing
question deadline. Identical requests reactivate their retained result rather than
repeating commands. The selected agent profile sets `evidence_result_chars`
(default 12,000) and `evidence_total_chars` (default 36,000). These are serialized
character allowances, not token counts, and do not change tool permissions,
request counts, or the question deadline. Tool payload packing follows the
per-result allowance while reserving 1,500 characters for its envelope.

The total allowance bounds each turn's evidence context, not a permanently
exhausted append-only transcript. A question-local workspace retains individually
bounded results and selects a working context again each turn. Deliberate reads
outrank discovery; recency breaks ties, and explicit reactivation takes priority
for the next selection. This is deterministic selection, not a semantic relevance
judge. Passage identity uses source/revision/range; repeated discovery candidates
are deduplicated. Evicted results retain compact recovery handles, not purported
facts. Final source reporting includes only selected evidence.

Oversized individual results are omitted with an explicit unknown status. Reads
provide bounded passages and continuation handles: `complete` means this call
supplied the entire selected range, whereas `at_end` only marks its final page.
Initial context retains its separate bound and does not automatically stuff wiki
searches into model-directed research. See [setup](Setup-and-Operations.md#evidence-allowances)
for configuration and privacy/performance tradeoffs.
If tool packing omits matching items, the result is `partial`, not `not_found`;
source diagnostics distinguish budget omissions from genuinely empty results.

All model adapters consume the same answer-or-request contract. Source text,
tool output, room descriptions, and item descriptions remain untrusted data.

## Research sources

Search snippets are discovery, not full-page evidence. Readable source snapshots
live outside the prompt for this question only. A stale selected GSWiki page can
be refreshed by exact title under configured online policy. If its revision
changes before reading starts, the same `knowledge.read` call supplies a bounded
passage from the replacement for a whole-page request or a uniquely matching
section path. Section identity includes heading levels, ancestor headings, and
whitespace-normalized, case-folded heading text; the path must be unique in both
snapshots. Missing, ambiguous, reparented, or level-changed sections instead return
`revision_changed` with a replacement handle and outline for deliberate selection,
without read evidence.

A successful refreshed read uses the replacement's source/section handles,
revision, provenance, and continuations, with `refreshed_from` metadata and a
`refreshed_read` diagnostic. Old continuations are never reinterpreted against
another revision. Once reading has started, the snapshot stays pinned for the
question. Failed or disabled verification leaves the stale label intact.

Markdown ownership is established by `characters/<name>.md` (including suffixed
topic filenames) or a `Character: <name>` metadata line. Mixed documents mentioning
other known characters are conservatively excluded. Unknown identities in free
prose cannot be inferred: correctly label private notes. General-web fallback is
discovery-only; it does not grant arbitrary webpage reading. Raw MediaWiki tables
retain their row/column syntax, but long tables may require preceding read pages
for context. This is not a rendered-wiki engine.

See the [implementation plan](Research-Workspace-Plan.md) for scope and verification.

## Character freshness

Recent same-session INFO/SKILLS observations can be reused. Missing, stale,
incomplete, or level-inconsistent categories may request registered
`character.recon`, with only the fixed `info` and `skills` commands.
Actions-off blocks execution, and approval remains independently required where
broker policy says so.

Recon requires a fresh starting snapshot, the bound generation and room, and
new completed observations followed by an advancing snapshot. Cached arrays,
unchanged timestamps, a sent result, or a model assertion cannot substitute
for that evidence. Skills must agree with the known observed level.

## Cancellation and persistence

Forget, timeout, and session replacement invalidate the question. Exact owned
pending recon is cancelled synchronously; late approvals cannot revive it.
Cancellation racing operation admission waits for the returned operation ID
to be revoked. Unrelated actions and successor operations remain untouched.
Already-dispatched commands cannot be unsent.

Only verified observations flow through the existing private persistence
adapter. The loop does not edit wiki pages or promote model output to truth.

## Verification

Tests cover direct answers, dependent/multiple sources, fresh reuse, rejected
arguments, whole-batch validation, actions-off, approval expiry, identity fences,
duplicates, budgets, injection-like source text, cancellation races, and
rendered provenance. Live tests require separate player authorization and must
report actual observed evidence, not just successful transport.
