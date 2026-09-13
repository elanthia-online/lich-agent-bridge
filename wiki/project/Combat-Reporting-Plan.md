# Combat recorder integration — first slice

Status: first slice implemented and offline-verified; one player-authorized live
trial passed on 2026-09-11. No further live deployment or combat test is
authorized by this document.

## Outcome

Attach bounded, attributable combat reports to existing EO Hunter trial results.
Reuse Nisugi's `Combat::Recorder` database and parsing; do not parse `cstats`
output or build another combat recorder. The player-facing script remains
independent. LAB continues to own authorization and experiment metadata; Hunter
continues to own combat, cleanup, and refuge return.

## Integration dependencies

- Lich supplies `Combat::Recorder`, trustworthy combat-observation provenance,
  and the optional post-commit `recorded_attack` receipt. The receipt identifies
  an exact committed row without changing the Recorder schema.
- EO Hunter retains the optional observation context captured when a controlled
  trial selects its target. Ordinary hunting remains independent of recording.
- The player starts and owns the recorder. LAB never starts, replaces, stops, or
  configures it. This first slice reads the conventional per-character
  `combat_stats.db` under Lich's data directory; it does not discover custom
  Recorder paths.

Missing receipt or context support makes reporting unavailable without changing
the controller outcome. These dependencies are deliberately maintained and
reviewed in their owning repositories rather than copied into LAB.

## Ordered work

1. Add a small optional core `recorded_attack` observer receipt after a successful
   recorder transaction. Include opaque recorder identity, database locator for
   trusted local code, row/session IDs, and existing validated ingestion source.
   Do not duplicate subscriptions, change the seven-table combat schema, or
   fabricate provenance for old/replayed events. Subscriber failures must not
   change recording or combat processing.
2. LAB subscribes once per admitted native controller run without starting,
   replacing, or closing another owner's recorder. Capture only bounded scalar
   receipts for the bound connection/character; unsubscribe the exact callback.
   Associate rows with native trial target, source room epoch, and monotonic
   receive window. Missing/ambiguous source is unavailable, not guessed.
3. After child cleanup, attach a compact report to the existing controller result.
   Read exact referenced rows from one read-only SQLite snapshot with a short
   query deadline and row/output caps. Verify recorder/session/character and
   target bindings. Never allow model-supplied SQL or filesystem paths. Failures
   affect report availability only, not the original test or recovery outcome.
4. Expose `combat.report` through LAB's existing read-only evidence interface and
   a bounded SessionHub/CLI lookup of retained operation evidence. Bind access to
   the selected character. Return source row references, observations, limitations,
   and the original operation/handoff result. Historical reports are not current
   character state. Report retention follows existing operation-history lifetime.
5. Verify offline with synthetic database/observer/transport fixtures, then stage
   a player-authorized one-creature trial after all matching revisions are ready.

## Measurement contract

- First slice: attributed own attack records, direct/flare damage to the selected
  target, recorded resolutions, foreign/unowned participation, and exact row refs.
- Keep native kill/target-lost outcome, elapsed time, and resource observations.
  Resource deltas are net observations, not proven spell mana expenditure.
- Attack records are not casts: triggered effects and damage-over-time events
  can create additional records. Do not divide mana by record count as cast cost.
- Partial data, missing receipts, query limits, unsupported schemas, and observer
  overflow are explicit. Zero observed events does not prove no attacks occurred.
- Source identity and timing establish observed association, not that a spell
  alone caused a kill. Group contributions and incidental attacks remain visible.
- No causal ranking, confidence scoring, whole-history search, new database,
  arbitrary report SQL, automatic recorder startup, or report-driven combat.
- Reports and raw character data stay private; public fixtures are synthetic.

## Verification and rollback

Prove post-commit receipts and absence on rollback; preserve existing recorder
tests. Exercise delayed events, wrong character/connection/room, duplicate rows,
two recorders, recorder replacement, read failure, overflow, empty observations,
foreign damage, and failed safe handoff. Test model argument validation and
cross-character report access. Exercise real SQLite schema, not only fake queries.

Run Hunter and LAB regression suites. Missing support must leave normal hunts and
existing LAB trials unchanged. Keep changes in isolated branches with separate
Lich/LAB review scopes; coordinate the receipt contract with Nisugi before public
integration. Do not modify his private script. Rollback removes the optional LAB
subscription/reader; recording and original controller results remain intact.

Live acceptance: start at a player-designated refuge, execute one already-reviewed
trial, finish at refuge with original equipment and owners released, and reconcile
reported rows with the recorder and visible game output. No live pass is claimed
until that sequence is observed.

## Implementation bounds and verification

Reports capture at most 128 receipts, return at most five trials and eight row
references per trial, and fit within 3500 characters. Overflow/omission is
explicit. Read-only SQLite queries use a 25ms lock/statement timeout and a 100ms
between-query deadline; platforms without `statement_timeout=` fail unavailable.
These are cooperative limits, not a hard real-time scheduling guarantee.

Offline verification includes the focused Recorder and Hunter suites, LAB's Ruby
bridge/report suites, the real core-to-LAB Recorder contract test, 775 Python
tests (one optional cross-language skip), and 27 MCP tests. Focused Ruby lint,
MCP type checking/declaration checks, package builds, and documentation links
pass. Public fixtures are synthetic.

Final integration checks also cover evidence priority under a tight context
budget and named historical source diagnostics (without claiming a reference
when no report exists). The staged MCP build passes its type/declaration checks.

## Live acceptance — 2026-09-11

After a logged-out backup/deployment, the player started the unchanged supplied
recorder in a fresh session. One registered single-target trial started and ended
at its configured refuge. Native cleanup, original equipment, survival and owner
release were verified; SessionHub accepted the terminal result without alerts.
The retained report was retrieved through the MCP SDK and matched the exact
Recorder attack/hit/resolution rows and visible game output. The player's
recorder remained running after LAB released its observer.

The kill outcome came from Hunter's trial observation and was corroborated by
the game log; the Recorder creature kill-link field was unset. This pass verifies
the bounded attack/hit/resolution association, not Recorder kill-link completion,
multi-trial comparisons, delayed effects, group attribution, or detailed status
and flare reports. Those cases remain offline-covered or unverified live as
applicable; do not generalize this one trial into a causal ranking guarantee.

The initial acceptance sequence exposed one core framing edge case: a complete
standalone room-object roster refresh could invalidate the provenance of the
first following attack even though it did not change rooms. The Lich-side repair
excludes only complete standalone room-object/player refreshes from the combat
buffer. Partial, nested, mixed-room, and navigation-bearing fragments remain
guarded. Synthetic production-hook regressions cover both sides of that boundary.

After that repair, a second authorized trial returned safely with original
equipment and released owners. Both delivered receipts were accepted: one
zero-damage inbound attack and the character's first outgoing cast. LAB's attack
count, damage, and exact row references matched a read-only Recorder query and
the visible game output. Recorder remained independently running after LAB
released its observer.

This verifies the first-cast reporting path for that outing, not every XML framing
pattern, group attribution, delayed effect, or multi-trial comparison. Mixed
accepted/rejected captures are reported as `partial`; unknown provenance remains
unknown rather than being inferred.
