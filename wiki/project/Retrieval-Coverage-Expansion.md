# Retrieval coverage expansion and recovery

Approved outcome, 2026-09-07: the maintainer requested a broader query corpus
and approved page-focused recovery, question-part coverage, and a small optional
semantic-search experiment. This extends the completed bounded passage-ordering
pass; it does not authorize deployment or changes to game/action authority.

## Sequence and gates

1. Preserve the original 12 real-snapshot queries and synthetic corpus unchanged.
   Add 36 independently source-grounded questions: 12 profession questions
   covering all ten professions, 12 equipment/crafting questions, and 12 society
   and general-system questions. Include lookup, multipart, and paraphrase
   groups. Freeze questions, literal evidence, source revisions and hashes before
   inspecting their retrieval outcomes. Keep real source fixtures private.
   Compare legacy and current PIF using the existing three-read strategy and
   6,000/10,500-character allowances. Report original and expansion separately,
   with category/query-type breakdowns, not just a combined percentage.
2. Recover relevant ranges within already-selected pages behind the existing
   knowledge interface. Bound any added work to selected snapshots; preserve
   revision/range identity, source eligibility and read limits. Test independently
   with synthetic pages before measuring the frozen corpora. No topic exceptions.
3. Select evidence that covers distinct parts of a question, including multiple
   relevant ranges from one source when justified. Preserve general source access
   and explicit omission diagnostics. Compare against the same deterministic
   reader; separately label any agent-directed selection experiment rather than
   replacing the existing score. No blanket budget increases.
4. Evaluate a small optional lexical-plus-semantic pilot on a fixed documented
   subset with distractor pages, using an isolated experiment environment.
   Record model/artifact identity, retrieval quality, latency and resource cost.
   Semantic candidates and reranking are distinct experiments. Do not install a
   mandatory runtime dependency or turn on a new provider without reviewing the
   measured tradeoff with the maintainer. No paid model or live-game calls.

Each implementation step needs actual-read regressions, unchanged legacy/gold
comparisons and a rollback decision for regressions. Do not tune against only
the original known failures. The expanded set is broader but hand-selected, not
a statistical sample of all player questions. Literal fact scoring can miss
equivalent supporting wording; keep that limitation separate from genuine
evidence loss. No promise of a target accuracy percentage or median speedup.

## Constraints and linkage

Retain one settings/knowledge interface, source provenance, freshness policy,
question ownership and cancellation. No wiki crawling, installed mirror changes,
deployment, new game commands, database migration, Git push or PR in this arc.
Any downloaded experiment model must remain isolated from runtime configuration.
Use source-backed plans/tests while Sophia tools are unavailable. The planning
and subagent workflow fallbacks establish bounded ownership and verification;
they do not claim available Sophia integration.

This approved scope extends [the PIF plan](Passage-Index-Plan.md), informed by
[its measured misses](Passage-Index-Benchmark.md), while retaining
[the shared evidence contract](Evidence-Gathering-Arc.md) and
[design decisions](Decision-Log.md). Completed benchmark rows are not rewritten.

## Frozen expanded corpus

The new set has 48 questions and 119 literal expected facts. The first 12
questions (33 facts) are unchanged; 36 new questions add 86 facts across all ten
professions, equipment/crafting, and societies/general systems. Each expansion
category contains four lookup, four multipart and four paraphrase questions.
Authors checked the exact source revision and literal text before any retrieval
scores were inspected. The controller validated the additions against indexed
snapshots and froze exact ranges, source fingerprints and corpus identity.

Expanded corpus SHA-256:
`a7e76f7612857f876951077b97e58d5f83029d7c6e01891a15c0ec3b6a1feb02`.

This is a hand-authored diagnostic corpus, not a representative random sample of
player traffic. Query-type labels are author-assigned: a lookup can request
several related facts, and paraphrase takes priority over multipart. A small
amount of gold is related context rather than explicitly requested detail (for
example, level-dependent symbol costs in the favor question). Literal substring
scoring can undercount equivalent evidence. Those limitations remain frozen and
visible rather than being corrected after seeing the scores. Snapshot evidence,
including obsolete-page warnings, is not verified current game mechanics.

## Page recovery and coverage results

Both allowances use the same isolated full mirror, fixed three-read round-robin
reader, empty curated roots and blocked networking. One first pass is excluded,
then one warmed iteration is recorded per mode and allowance. The prior code was
copied before implementation. No gold edits, extra read calls, schema changes,
new discovery lanes or ranker topic exceptions were introduced.

| Corpus slice | Prior PIF facts | Recovery + coverage facts |
| --- | --- | --- |
| Original 12 | 22/33 | 25/33 |
| Added 36 | 34/86 | 49/86 |
| All 48 | 56/119 (47.1%) | 74/119 (62.2%) |
| Added professions | 12/31 | 17/31 |
| Added equipment/crafting | 13/29 | 18/29 |
| Added societies/systems | 9/26 | 14/26 |
| Added lookups | 16/25 | 16/25 |
| Added multipart | 18/33 | 30/33 |
| Added paraphrases | 0/28 | 3/28 |

Fact scores are identical at 6,000 and 10,500 characters. Nine questions gain
facts, none lose facts; completely covered questions increase from 19/48 to
26/48. Legacy remains 54/119 with 36/50 expected sources. PIF source coverage is
unchanged by these fixes: 33/50 at 6,000 and 34/50 at 10,500. This separates better
reading of selected pages from finding missing pages. The original synthetic
corpus increases from 13/16 to 14/16 indexed facts; legacy stays 10/16, with
8/9 expected sources for both paths.

| Allowance | Prior PIF median / p95 | New PIF median / p95 | Same-run legacy median / p95 |
| --- | --- | --- | --- |
| 6,000 | 130.8 / 815.0 ms | 139.2 / 825.6 ms | 151.6 / 1,028.0 ms |
| 10,500 | 138.9 / 837.0 ms | 139.2 / 836.2 ms | 143.7 / 952.3 ms |

These are search-plus-read measurements, not model-answer latency. The new work
adds bounded local scoring; it is not a demonstrated speedup over prior PIF.
Only one measured iteration per question was collected, and cache/machine load
varied. No statistical latency claim or comparison to previous five-iteration
runs is warranted. Combining nearby passages uses more of the existing read
capacity, not a larger allowance; it preserves all intervening source text.

The independent boundary review found an intervening oversized-table warning
could be lost when composing a window. The fix reuses the indexer's structure
rules on the read-sized merged window. A regression verifies the complete table,
its qualifiers and its warning. A separate merged-handle test verifies that a
changed live revision never reinterprets the old offsets.

Decision: retain the bounded recovery/coverage changes for further integration
testing. They improve actual evidence delivery on both frozen corpora without
case-level losses. They do not solve missing-page discovery: most descriptive
paraphrases still fail, and neither larger allowance changes that.

## Optional semantic pilot

The isolated experiment uses
[all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
at artifact revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`.
It runs local ONNX inference with attention-masked mean pooling and L2
normalization. Token windows cover every passage token rather than silently
dropping long tables; 32-token overlap and at most 64 title/heading tokens leave
each model input within 256 tokens. Window scores select original revision-bound
passage ranges, not fabricated excerpts or reconstructed tables.

The fixed manifest contains 128 pages: all 47 required source pages plus 81
distractors sampled from sorted eligible titles with seed 20260907. It was frozen
before inference, SHA-256
`ae1495c26b81631a8b1666761572815e544be6a3cba9d2897914bcd9f970f086`.
All required revisions and 119 literal facts were verified. No source was skipped.
This deliberate answer-source inclusion makes the task easier than full-wiki
discovery. The subset BM25 control also differs from production PIF: no title
lane, production source ranker, focused page recovery or coverage windows. All
three pilot modes use the same subset, original passage ranges, genuine research
packing/reads, and three-read strategy. Do not compare their percentages directly
to the 74/119 production-path result.

| Subset mode | Facts delivered | Complete questions | Added paraphrase facts |
| --- | --- | --- | --- |
| Subset BM25 | 72/119 (60.5%) | 24/48 | 13/28 |
| Lexical + semantic union, reciprocal-rank fusion | 82/119 (68.9%) | 28/48 | 15/28 |
| Semantic rerank of the lexical candidate pool | 91/119 (76.5%) | 30/48 | 22/28 |

Scores are identical at both allowances. All modes already contain 50/50
required source occurrences in their top-36 candidate pool, so this demonstrates
better ordering/packing in this subset, not new-page recall. At 6,000 characters,
served-source coverage is 48/50 for BM25 and 50/50 for both semantic modes.
Each mode performs 144 reads; one BM25 read and two reads in each semantic mode
need continuation. Unreturned continuation text does not count toward scores.

There are regressions, not just gains. Union improves six questions and worsens
one, net +10 facts. Reranking improves 13 and worsens five, net +19 facts; affected
topics include infusion, herb production, fusion, society membership and guild
tasks. Therefore the aggregate win is not a safe unconditional replacement for
lexical ordering. No model choice, fusion constant, ranking or gold was tuned
after these outcomes.

Resource measurements on two CPU threads:

- 1,373 original passages produce 2,475 token windows.
- Initial subset loading/embedding preparation: 95.2 seconds; model load: 259 ms.
- Vectors: 3,801,600 bytes (3.63 MiB); measured process peak RSS: 760 MiB.
  Vector size is not total model/runtime memory.
- Query embedding plus scoring the whole subset: median 24.6 ms, p95 43.5 ms at
  6,000; median 26.8 ms, p95 50.8 ms at 10,500. SQL and evidence reading are
  separately measured, so these are not total request or agent-answer latencies.
- The second allowance reuses embeddings in the same isolated process, with
  passage identity validation. No persistent runtime cache or provider was added.

Model SHA-256:
`6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452`.
Tokenizer SHA-256:
`be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037`.
The run used numpy 2.5.3, onnxruntime 1.29.0 and tokenizers 0.23.2 in a temporary
virtual environment. The wrapper blocked Python socket connections; model
artifacts were downloaded beforehand and telemetry was disabled. The runtime's
telemetry-ID persistence warning did not fail inference. No network fallback,
paid model or game command was used in any measurement.

Reproduction entry point: `scripts/benchmark-semantic-retrieval.py --help`.
Supply a copied indexed database, corpus in the lexical benchmark's question
format, a JSON page-title list (or object with `titles`), and a local model
directory containing `model.onnx` and `tokenizer.json`. Install optional packages
in an isolated environment, not LAB's runtime. The harness never downloads a
model and its synthetic tests require none of those optional packages. Private
artifacts retain the exact manifest, corpus, input checksums, run wrapper and
both reports; real wiki excerpts are not added to public fixtures.

Decision: the pilot justifies considering a separate larger reranking evaluation,
with explicit case-level regression gates and realistic full-wiki candidates.
It does not justify enabling semantic search by default. That integration and
any additional full-wiki semantic index remain follow-up scope, not implemented
or authorized by this experiment's outcome.

## Verification and closeout

632 Python tests, 149 Ruby tests (714 assertions), and 24 MCP tests pass.
MCP type checks, generated SDK declarations and build pass. All 55 Markdown
files pass local-link checking, and Git diff whitespace checks are clean.
The full Python/MCP suites used approved local socket access; they did not run
against an installed character or game session. Eleven new actual-read/recovery
boundary tests and five optional-harness tests supplement the earlier PIF suite.

The codebase-design and bounded subagent-development workflows kept page
recovery behind the existing knowledge interface and assigned independent
source-authoring, implementation and boundary-review work. Sophia tools were
unavailable; source, reproducible fixtures and local tests supplied the evidence.
The controller reviewed the bounded worker changes and semantic harness; the
one confirmed window-metadata defect was reproduced and corrected without
expanding the feature scope.

Private archives retain both code states, frozen inputs, raw actual-read reports,
semantic reports and replay scripts. The installed mirror's original SHA-256
and frozen corpus hashes remain unchanged. No deployed service, game state,
production model setting, database schema or installed mirror was modified.
There is no live-answer accuracy claim, Git commit, push or PR from this pass.

## Approved follow-up: realistic full-wiki candidates

The maintainer authorized this next experiment after the subset results. Freeze
the same 48 questions, gold, model artifacts and source snapshot. Use the actual
full-wiki PIF candidate pool (up to 36 admitted ranges per query), without adding
answer pages or restricting search to a favorable manifest.

Compare the unchanged production lexical pipeline with semantic source ordering,
then source plus recovered-passage ordering. This separates where reranking helps
or hurts. Source similarity is the maximum score among that source's admitted
passages; original order breaks ties. Within a selected page, semantic ordering
may reorder only its existing focused candidates. Keep the current coverage
window, maximum six selected sources, three reads, both allowances and provenance
checks. No new retrieval lane, reranking weight tuning or model selection sweep.

An experiment-only embedding cache can reuse exact range identities, but each
query must retain its own discovered candidate membership. Report preparation
and warm scoring costs separately; cached setup costs must not masquerade as
uncached request latency. Verify baseline evidence scores before interpreting
semantic results. Report candidate availability separately from served sources
and actual read facts, including per-question regressions. Do not claim that a
reranker can recover a page missing from its candidate pool.

This authorization covers benchmark tooling, isolated local inference, tests and
documentation only. Production code/configuration, installed mirrors, live-game
actions, full-wiki vector indexing, commits, pushes and deployment remain out of
scope. No automatic integration follows from a positive aggregate result.

### Full-wiki method and verification

The experiment used the unchanged production search/read implementation against
the entire copied 33,107-page mirror. Independent pre-inference checks verified
all 48 questions' original candidate identities and exact baseline served-title
orders, read status, revision, ranges, completeness and text hashes at both
allowances. Gold was used only for verification and reporting. Candidate discovery
and model inputs did not consult expected titles or facts.

The optional harness is `scripts/benchmark-fullwiki-rerank.py`; its context-managed
experiment hooks change ordering only. Query-specific admitted ranges vote for
their source; ranges cached for other queries cannot affect that vote. Focused
passage membership is checked before any permutation. Source-only mode retains
the lexical focused ordering and coverage window. Production runs repeat after
embedding preparation, alongside semantic runs, and must still match the exact
verified baseline. Timing uses one fixed mode order, not randomized repetitions.

The first preparation attempt stopped before model creation on one 1,833,454-
character storyline log. Production already rejects that page at registration.
The corrected harness retains its admitted scoring ranges but does not perform
focused recovery that the production pipeline cannot reach. It records the
ineligibility explicitly; no question or original candidate is removed. A new
test forces that page to the highest semantic score and verifies it still cannot
be focused or read. No read limit was increased.

The independent full-wiki candidate ledger contains 45/50 required source
occurrences, not the favorable subset's 50/50. Across all admitted ranges, 87/119
literal facts are available somewhere; focused recovery across all candidate
pages exposes 100/119. These are diagnostic availability counts, not facts
actually delivered within the three-read budget and not a model-answer ceiling.

### Full-wiki results

| Allowance | Mode | Facts delivered | Complete questions | Served required sources |
| --- | --- | --- | --- | --- |
| 6,000 | Production lexical | 74/119 (62.2%) | 26/48 | 33/50 |
| 6,000 | Semantic sources only | 85/119 (71.4%) | 32/48 | 40/50 |
| 6,000 | Semantic sources + passages | 87/119 (73.1%) | 33/48 | 40/50 |
| 10,500 | Production lexical | 74/119 (62.2%) | 26/48 | 34/50 |
| 10,500 | Semantic sources only | 87/119 (73.1%) | 33/48 | 43/50 |
| 10,500 | Semantic sources + passages | 87/119 (73.1%) | 33/48 | 43/50 |

At the normal 10,500-character research allowance, both semantic variants improve
nine questions and regress one. Added paraphrase coverage rises from 3/28 to
11/28 facts; expansion coverage rises from 49/86 to 62/86. The original corpus
remains 25/33 in aggregate, but that masks a two-fact gain on the descriptive
sonic/UAC question and a two-fact loss on its explicit-name counterpart.

The regression is significant to retain: for the explicit sonic-paingrip/UAC
question, semantic source ordering drops the specific Paingrip reference from
the served list and promotes broader bard guides. The fixed reader then receives
none of that question's three gold facts instead of two. The needed source was
in the original candidate pool; this is a source-ordering regression, not a
missing-index-page problem. No topic exception or post-result rank tuning was
added to conceal it.

Both semantic modes use 144 reads per allowance, just like production. At 10,500,
production has one incomplete read, source-only has three, and combined has two;
unreturned continuation text never counts as evidence. At 6,000, those counts
are five, four and three respectively. This remains an offline fixed-reader
retrieval test, not a live agent-answer accuracy test.

### Full-wiki costs and decision

Preparation loads 1,066 unique candidate snapshots (20,566,919 normalized
characters) and selects 3,451 unique original ranges. The largest range is
68,403 characters and remains covered by token windows; no table truncation
was introduced. Range preparation takes 4.0 seconds, local model load 439 ms,
and embedding 6,412 windows takes 188.5 seconds on two CPU threads. Vectors occupy
9,848,832 bytes (9.39 MiB); peak process RSS is 835,992 KiB (816.4 MiB). The complete
experiment, including verification and both allowances, takes 308.6 seconds.

Warm query encoding plus scoring the cached union ranges takes a median of
9.4–9.9 ms and p95 about 12 ms. Scores are then fenced to the current query's
candidates. At 10,500, the measured search/read median including semantic work is
154.5 ms for source-only and 155.4 ms for combined, versus 159.5 ms for production.
Their p95 values are 859.6, 857.7 and 842.1 ms respectively. These are exploratory
single-order measurements, not a demonstrated speedup. Offline embedding costs
are not included in those warm request figures; this is not a production cache
implementation or an uncached latency claim.

Decision: semantic source ordering has a real aggregate benefit on actual
full-wiki candidates. The extra passage reranking adds no fact coverage at the
normal allowance and adds only two facts at 6,000, so source-only is the simpler
candidate for a future integration. However, unconditional semantic replacement
still fails the explicit-reference regression. A separate follow-up could test
preserving strong explicit lexical matches while using semantic order for the
rest. That policy, a runtime embedding cache/provider, and deployment are not
implemented by this experiment.

Seven new synthetic harness tests and the full 639-test Python suite pass;
local documentation links and diff checks are clean. Production retrieval files,
installed mirror and frozen corpus hashes are unchanged. The bounded independent
review verified membership/gold isolation and prompted the matched-warm baseline
check. Private reports preserve raw reads, candidate ledgers, failed preparation,
pinned wrapper, model hashes and summaries. No new model download, live-game
action, production setting, commit, push or deployment occurred.
