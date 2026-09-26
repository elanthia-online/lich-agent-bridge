# Full-mirror PIF benchmark

Measured 2026-09-07 on isolated copies of the 2026-08-30 local GSWiki mirror.
No installed database, configuration, service, model, or game session changed.
The source database's SHA-256 was unchanged before and after the experiment.

## Storage and build

- 33,107 mirrored documents, 190,227 indexed passages; no excluded documents.
- 30 empty normalized documents and 10,573 flagged oversized structures.
- Existing research namespace policy allows 30,552 documents (namespaces 0/4).
- One normalized document exceeds the existing 1,000,000-character read limit.
- Database: 552,632,320 bytes before, 1,181,437,952 bytes after (about 2.14x).
- Full offline atomic build: 47.52 seconds; process peak RSS 71,632 KiB.
- Unchanged atomic rebuild: 8.80 seconds, zero documents normalized and all
  33,107 reused. This still copies, validates and publishes a database.

The source mirror used WAL. SQLite's backup interface produced isolated copies;
only those copies were converted to DELETE journaling for the offline builder.
Do not run the current offline builder against an active WAL mirror.

## Method

An independent worker froze 12 questions against 11 exact-title reference pages
before seeing retrieval results. They cover sonic/UAC interactions, 706, scroll
infusion, spirit recovery, cleric CS, empath herbs, sanctuary, enchanting,
foraging, and positioning. Two paired paraphrases are deliberate challenges.
The controller independently verified all 33 literal expected facts and page
revisions against the copied normalized snapshots.

Frozen corpus SHA-256:
`fa342c49bb10bccbe91d42696b9a06ac8608b548e418240d503f5c3ba520bb00`.
Private artifacts preserve exact questions, expected facts, issued handles,
returned reads, timings and scripts; they are not shipped as public fixtures.

Both paths used the same fixed three-read round-robin strategy from the synthetic
benchmark, new research sessions per question, empty curated roots, and disabled
network fallback. Socket calls were additionally blocked. Scoring counts only
literal facts in returned read text, not snippets or entire stored documents.
Expected-source recall uses canonical titles, not equivalent alternative pages.

The primary run retained the prior benchmark's 6,000-character research allowance:
one uncounted first pass followed by five warmed iterations. A second diagnostic
run used LAB's default 10,500-character research allowance (12,000 minus the
1,500-character envelope reserve), with one first pass and one measured iteration.
No settings, ranks, budgets or gold facts were changed in the application.

## Results

| Research allowance | Path | Facts delivered | Expected sources | Median search + read | p95 |
| --- | --- | --- | --- | --- | --- |
| 6,000, five iterations | Legacy | 17/33 | 10/14 | 188.6 ms | 1,253.3 ms |
| 6,000, five iterations | PIF | 19/33 | 8/14 | 475.0 ms | 1,517.5 ms |
| 10,500, one diagnostic iteration | Legacy | 19/33 | 11/14 | 154.8 ms | 983.6 ms |
| 10,500, one diagnostic iteration | PIF | 17/33 | 10/14 | 446.4 ms | 1,429.3 ms |

Do not compare timings across allowance runs as controlled performance effects;
the second run has fewer samples and was later in the process. Neither is a
cold-cache test: copying and indexing already accessed the full corpus.

PIF's primary-run local discovery median was 464.5 ms; selected document loading
was 3.5 ms and ranking 1.8 ms. These separately measured medians are not additive.
They locate the dominant measured cost in local discovery rather than selected
document loading; they do not establish which SQL operation needs changing.

At 6,000 characters, PIF improved the enchanting case but lost the canonical
sonic-weapon and scroll-infusion sources from the delivered shortlist. At the
default allowance those sources returned, but the expected UAC positioning page
remained absent under PIF while legacy returned it. Both paths missed both
paraphrase challenges. The fixed reader's coverage also changes with candidate
packing: more exposed candidates can consume reads that otherwise reach another
passage. These are retrieval/selection probes, not model answer-accuracy scores.

## Decision and limits

The full index builds, but this implementation is **not ready to deploy as a
performance improvement**. Tiny-fixture gains did not generalize reliably.
Proposed next scope is a focused discovery-cost and source/handle-preservation
pass, evaluated against the unchanged corpus and allowances. Investigate query
cost separately from source ranking and response packing. Do not hide regressions
by silently raising budgets or tailoring rules to these questions.

Semantic retrieval remains a later measured experiment, not an assumed cure for
these lexical regressions. No embedding dependency, production migration,
commit, push, or live-test claim was made. This small hand-selected regression
set is not representative of the entire wiki, its paired paraphrases are not
independent, and snapshot revisions are not verified current game mechanics.

## Focused discovery fixes — 2026-09-07

The maintainer authorized a bounded cost/source-preservation pass. The original
measurements above remain unchanged. No source-ranking weights, embedding model,
index schema, source eligibility or evidence allowances were changed.

Two independently reproduced causes were fixed:

1. The joined SQL result had an additional global sort before LIMIT. Using FTS's
   native rank cursor removes that joined sort. General results can also supply
   the non-catalog lane when both independent-source and range-slot requirements
   are already met; otherwise the independent lane still backfills. Synthetic
   SQLite VM work fell from 69,600 to 21,600 operations. EXPLAIN no longer showed
   the joined temporary ORDER BY tree. This does not eliminate FTS's internal
   posting-list ranking or promise constant-time search for common terms.
2. Greedy outlines and duplicate previews displaced later useful sources.
   Packaging now reserves each admitted source's best read handle before extra
   outlines, shares remaining space across sources, and avoids a duplicated first
   passage preview. Tight previews carry ellipses; provenance and ranges remain
   intact. Six remains the maximum, not a guarantee at arbitrarily small budgets.

All six source-preservation checks now pass: the sonic-weapon, scroll-infusion,
and UAC positioning sources are delivered at both 6,000 and 10,500 characters.
The one-query exploratory 1.2x-legacy timing probe still reported 1.26x (182 versus
145 ms), improved from 2.53x; that probe was not relaxed or called green.

Both allowances were then rerun for five warmed iterations on the same copied
databases and frozen question hash, with the same fixed three-read reader.
Legacy also receives the shared packaging fix, so its new measurements are
shown rather than attributing all packaging gains to the passage index.

| Allowance | Path with fixes | Facts delivered | Expected sources | Median search + read | p95 |
| --- | --- | --- | --- | --- | --- |
| 6,000 | Legacy | 19/33 | 11/14 | 145.0 ms | 975.6 ms |
| 6,000 | PIF | 17/33 | 11/14 | 160.2 ms | 875.8 ms |
| 10,500 | Legacy | 19/33 | 11/14 | 148.8 ms | 979.2 ms |
| 10,500 | PIF | 17/33 | 11/14 | 176.6 ms | 930.6 ms |

Compared with the first PIF run, median retrieval fell about 66% at 6,000 and 60%
at 10,500 characters. The same-run legacy median remains lower, while PIF's p95
is lower on this corpus. Absolute timings remain machine/cache-dependent.

Source preservation is repaired, but the fixed reader still delivers fewer facts
than legacy. For example, the cleric question now exposes more independent
sources, using a read that previously reached a second passage on the first page.
The indexed UAC page is present, but its first matched passage does not contain
the canonical positioning facts. Both paraphrase questions remain misses. These
results separate source availability from useful passage selection; they are not
a claim that model answer quality improved. The tiny synthetic corpus remains
10/16 legacy and 13/16 indexed, underscoring its limited predictive value.

Verification: 611 Python tests, 149 Ruby tests/714 assertions, and 24 MCP tests pass;
MCP type checks/generated declarations/build also pass. Eight new synthetic
regressions cover query cost, filters-before-limit, non-catalog/range backfill,
source packing, readable handles and tight-budget omissions. Debugging/design
skills anchored the changes to failing tests behind the existing knowledge
interface. Original mirror and frozen corpus checksums are unchanged.

Deployment remains deferred: lower discovery cost and restored source recall
are verified, but a net evidence-delivery win is not. Further passage selection
or semantic work is a separately scoped follow-up, not silently added here.

## Focused passage-selection follow-up — 2026-09-07

The maintainer approved the next selection pass with the same frozen corpora,
three-read strategy and allowances. Tracing the two focused failures found the
needed child passages already admitted, but behind an introduction or an unrelated
subsection. The first reads missed all five facts; later matching handles were
also displaced during packing. Increasing source recall alone had not solved this.

The change reorders only each source's already-admitted ranges by distinct query
matches in their heading paths, excluding page-title terms. BM25 breaks ties.
Source slots, passage membership, SQL, schema, bounds, packing and read behavior
remain unchanged. This is a lexical relevance heuristic, not an authority score.
No additional retrieval lane or topic-specific rule was introduced.

Two synthetic actual-first-read regressions failed before the fix and pass after
it. Three additional guards cover title-only/body-only queries and repeated
heading words. An independent five-case synthetic probe passes 5/5 versus 4/5
with the prior rerank-free behavior replayed in memory. That probe was authored
independently of the real corpus, but first executed after the fix landed; it is
not claimed as a preimplementation holdout. Table qualifiers and multiple-source
reads remain intact. The controller independently reran both probe modes.

Both full-corpus allowances again used five warmed iterations, socket blocking,
empty curated roots and unchanged database copies. These measurements completed
before the broader test suites were run.

| Allowance | Path | Facts delivered | Expected sources | Median search + read | p95 |
| --- | --- | --- | --- | --- | --- |
| 6,000 | Legacy | 19/33 | 11/14 | 171.2 ms | 1,191.9 ms |
| 6,000 | PIF | 22/33 | 11/14 | 210.7 ms | 1,306.0 ms |
| 10,500 | Legacy | 19/33 | 11/14 | 152.6 ms | 1,053.3 ms |
| 10,500 | PIF | 22/33 | 11/14 | 173.5 ms | 918.1 ms |

PIF's actual delivered facts rise from 17/33 to 22/33 at both allowances. The
cleric case improves 0/3 to 3/3, and UAC positioning 0/2 to 2/2. No other case
loses facts; exact-source recall remains 11/14. The original synthetic corpus
remains 13/16 indexed versus 10/16 legacy, with 8/9 sources for both.

This pass establishes a small-corpus evidence-delivery improvement, not faster
search. Timings vary across runs, including the unchanged legacy control. PIF
remains slower at the median, and its p95 is lower than legacy only in the
10,500-character run here. The change adds bounded in-memory ordering, no SQL;
the existing synthetic SQL-work guard still applies. Neither warmed retrieval
latency nor literal-fact coverage is end-to-end model answer performance.

Both paraphrase questions still miss. Partial misses remain in sonic/UAC,
infusion, spirit recovery and herb-production questions. Needed ranges outside
the admitted shortlist cannot be recovered by this ordering, and the fixed
reader still spends requests across sources instead of making model-directed
follow-up choices. Do not silently broaden this fix into semantic retrieval or
relax the scoring to hide those limits. No deployment, database rebuild, live
game test, commit or push was performed.

Verification: 616 Python tests, 149 Ruby tests/714 assertions, and 24 MCP tests
pass. MCP typecheck, generated declarations and build pass; 54 Markdown files
have no broken local links, and diff checks are clean. Socket-dependent suites
initially hit sandbox restrictions and passed in approved local reruns outside
the sandbox. The installed mirror and both frozen corpus hashes are unchanged.

## Expanded coverage follow-up

The maintainer subsequently approved a broader 48-question corpus, page-focused
recovery, bounded question-part coverage and a small optional semantic pilot.
See [the separate expansion report](Retrieval-Coverage-Expansion.md) for the
frozen corpus, before/after results and decision. The earlier measurements above
remain historical results; the expanded denominator is not interchangeable with
the original 33 facts.
