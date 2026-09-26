# Passage-index-first GSWiki retrieval

Status: optional runtime semantic integration authorized, 2026-09-07; not deployed.
The original PIF scope below is retained as history. Following frozen full-wiki
experiments, the maintainer authorized finishing semantic mode in the same PR.

## Authorized semantic completion

Keep one knowledge interface and default lexical retrieval. A single optional
local model-directory setting enables pinned MiniLM source-only reranking of the
existing 36-hit pool. Explicitly named references remain lexical anchors; curated
scopes, focused passage selection, six-source admission, freshness, read budgets,
and action authority stay unchanged. No online model download or vector service.

Use bounded per-process window-vector caching, lazy optional dependencies,
non-queuing inference, cancellation checks, and explicit whole-attempt lexical
fallback when unavailable or over allowance. Doctor checks local readiness without
inference. Benchmark the actual runtime on the same frozen 48-question/119-fact
corpus, including cold/warm costs and per-question regressions, then prepare one
combined PIF/semantic PR. No deployment, game commands, or corpus/gold changes.

Ownership: backend worker owns the encoder/cache; settings worker owns the one
setting, doctor, and runtime measurement harness; controller owns source-order
integration, exact-name protection, integration tests, docs, and final review.

## Original PIF scope

The maintainer selected a local
passage-index-first (PIF) approach; a semantic/hybrid comparison on a small fixed
corpus is a later, separately measured step, not part of this implementation.

## Outcome and scope

Use the existing local SQLite mirror to find directly readable evidence ranges,
not only relevant page titles. Keep the existing `knowledge.search` and
`knowledge.read` interface, model adapters, question ownership, source freshness,
and evidence/action limits. No embedding model, vector service, new runtime
dependency, paid inference, game interaction, or production migration is included.

All text pages in the selected mirror are eligible for derived indexing. This
does not claim completeness against the live wiki or expand the configured
namespace crawl. Research retains its current namespace policy (0 and 4), with
coverage reported separately from stored page counts. Templates are not expanded
as rendered MediaWiki content, and existing source-size/read-budget limits remain.

## Implementation contract

1. Normalize document text once per changed revision/content/normalizer version.
   Store deterministic heading paths and contiguous passage ranges against that
   exact text. Keep paragraph context and table structure; explicitly label
   oversized structures rather than manufacturing truncated table rows.
2. Use passage FTS alongside exact title/identifier recall. Rank bounded metadata
   before loading selected document snapshots. Preserve independent sources and
   multiple useful matches while suppressing redundant overlapping ranges.
3. Discovery exposes matched previews and opaque, directly readable passage
   handles through the existing section selection interface. Label their kind
   so callers do not mistake chunks for structural wiki sections. Existing whole
   page/structural-section reads remain available.
4. Every range stays bound to its document revision and normalized text.
   A changed revision cannot reinterpret old offsets or use structural-path
   matching for arbitrary passage chunks. Return explicit replacement information
   when a passage cannot safely retain identity. Already-started reads stay pinned.
5. Index building is explicit, offline, and atomic. Failure preserves the usable
   original mirror. Normal sync maintains derived records in its existing staging
   copy; unchanged content is reused, changed/deleted content invalidates old
   ranges. Querying never migrates or modifies a mirror.
6. Old or unavailable derived indexes fall back to the existing page search with
   a diagnostic. `labctl wiki index` builds locally without a network refresh;
   `labctl wiki status` exposes derived-index readiness and coverage.

## Ownership and verification

- Index worker: schema, normalization, chunking, rebuild/sync, index tests.
- Retrieval worker: knowledge/research integration, issued passage handles,
  revision-safe reads and prompt-delivery regressions.
- Controller: CLI, fixed synthetic benchmark, documentation, source review and
  integrated verification. No worker changes installed code or private records.

Tests cover actual search/read behavior, not just successful SQL calls: late
facts beneath generic headings, multiple rules, exact identifiers, archive and
catalog discovery, tables and qualifiers, redirects, changed revisions, missing
indexes, private scope, cancellation, byte/character ranges and budget packing.
Index tests also exercise idempotence, invalidation, failure preservation and
read-only query paths. Existing Python/Ruby/MCP contracts must remain green.

The benchmark uses independent synthetic pages and fixed questions with expected
facts. Compare legacy and indexed discovery with the same deterministic read
strategy and read budget. Report actual supplied-passage coverage separately
from source recall and warmed search/read latency. A correct page title is not
evidence that its needed paragraph reached the answer. Timing on a small fixture
does not predict full-corpus or model response time; semantic paraphrase misses
remain visible for the later optional comparison. No correctness oracle selects
read handles on the benchmark's behalf.

## Release gates

Complete offline integration before offering a backed-up local index build and
live answer comparisons. Do not silently redeploy, crawl the wiki, increase
model budgets, close passage-selection issue #7, commit, or push. Any full-corpus
benchmark must use a separate local copy and disclose what was measured. The
semantic experiment must reuse a fixed held-out corpus and report benefit,
latency, storage and model costs before becoming a product dependency.

The codebase-design skill keeps indexing behind one knowledge interface.
Sophia workflow tools are unavailable in this session; bounded subagent work and
source/test verification supply the documented fallback, with one final closeout.

## Offline verification results

The frozen synthetic corpus contains 10 pages and 9 questions, including one
deliberate vocabulary-mismatch challenge. With five warmed iterations and the
same three-read round-robin reader, legacy discovery supplied 10/16 expected
facts (62.5%); indexed discovery supplied 13/16 (81.25%). Source recall remained
8/9. Excluding the explicitly marked paraphrase challenge, fact coverage was
10/15 versus 13/15. These are substring checks on returned read text, not answer
accuracy or real-GSWiki completeness.

The final run measured median search-plus-read latency of 0.959 ms legacy versus
2.041 ms indexed, and p95 of 2.685 versus 3.341 ms. Synthetic database sizes were
94,208 versus 221,184 bytes. Small-file
measurements do not establish a full-corpus speedup or storage multiplier.

The indexed late-page and shopping cases improved. The multi-fact question still
failed the fixed reader: both fact handles were issued, but the reader spent its
three reads on the overview and two catalog candidates. The paraphrase case
remains a miss in both modes. Keep these limitations visible for the later
selection/semantic comparison; neither corpus nor gold facts were tuned to pass.

Rebuild requires offline writers and DELETE journaling; WAL mode is rejected
without changing the original. Oversized tables retain their complete flagged
range, but previews are limited to the first 1,800 characters and full reads may
require continuations. No installed mirror or runtime was modified.

Verification: 603 Python tests, 149 Ruby tests (714 assertions), and 24 MCP tests
passed; MCP type checks/generated declarations/build passed, and 53 Markdown
files had no broken local links. Two implementation workers completed; the
controller integrated the CLI/benchmark, reviewed the diffs, and ran the suites.
The subsequent [full-mirror benchmark](Passage-Index-Benchmark.md) found slower
discovery and source-selection regressions. Its copied index is complete, but
deployment is deferred pending a focused fix; no live evaluation, commit or push
was made. The synthetic results above are retained unchanged, not substituted
for the less favorable full-corpus measurements.

## Approved regression-fix slice

On 2026-09-07 the maintainer approved fixing measured discovery cost and
source/handle preservation before deployment or semantic experiments. Keep both
frozen corpora, research allowances, three-read comparison strategy, source
authority and query ownership unchanged. No game-topic exceptions or larger
budgets are authorized as a shortcut.

The controller reran a focused full-copy reproduction: the sonic/infusion sources
were omitted at 6,000 chars, the positioning source at both 6,000 and 10,500, and a
fixed identifier query took 2.53x legacy time. Workers own independently testable
query-cost and result-packing fixes; the controller verifies original failures,
complete corpora and regression suites before integration handoff.

Acceptance requires restoring the observed source omissions, showing reduced
measured discovery cost without changing source eligibility, and retaining useful
read handles under the same allowances. Report any remaining fixed-reader fact
misses rather than equating source recall with successful evidence delivery.
Changes stay within the current SQLite and knowledge interfaces. Full-corpus
measurements use isolated copies; no automatic migration, deployment or push.

Closeout: both workers completed the query-cost and source-packing fixes. The
controller verified all original source omissions are resolved at both allowances,
eight new red-to-green synthetic regressions, 611 Python/149 Ruby/24 MCP tests,
and fixed-corpus reruns. PIF median fell to 160–177 ms from 446–475 ms; the same-run
legacy path remains 145–149 ms. Fixed-reader fact coverage is 17/33 versus legacy 19/33.
Thus cost and source preservation improved, not overall evidence-delivery quality.
See the [benchmark follow-up](Passage-Index-Benchmark.md#focused-discovery-fixes--2026-09-07).
No deployment, schema rebuild, semantic dependency, private-state change or push.

## Approved passage-selection follow-up

The maintainer approved a further bounded selection pass on 2026-09-07. The
unchanged full-copy probe finds the intended cleric and unarmed-combat pages,
but its three actual reads supply 0/3 and 0/2 expected facts respectively.
Investigate passage recall/order separately from output packing; finding the
page is not the acceptance criterion. Add independent synthetic regressions
before changing selection, then rerun both frozen corpora and both existing
6,000/10,500-character allowances with the same reader and scoring.

Keep improvements generic and behind the existing knowledge interface. No
topic-specific weights, corpus/gold edits, larger budgets, semantic dependency,
full-corpus query-time parsing, game actions, installed-state changes, deployment,
commit or push belong to this pass. Report regressions and remaining misses,
including paraphrases, even if aggregate coverage improves.

Controller closeout: two bounded workers traced passage order versus packing
and implemented/reviewed a per-source heading-focus rerank. The controller
verified the original five missing facts now reach actual reads, independent
synthetic baseline/current probes, both unchanged full-corpus allowances and
the unchanged synthetic benchmark. Full-corpus PIF rises 17/33 to 22/33 facts
versus legacy 19/33, with source recall unchanged and no per-case losses.
616 Python, 149 Ruby/714 assertions and 24 MCP tests pass; typecheck, generated
declarations/build, 54-document link check and diff check pass. Socket-dependent
tests required approved local reruns outside the sandbox. No median-speed claim:
default-allowance medians are 173.5 ms PIF versus 152.6 ms legacy. Paraphrase and
partial evidence misses remain; deployment/semantics need their own next step.
Original mirror and corpus hashes are unchanged. Sophia remains unavailable;
this source-backed closeout records the verified handoff instead. No deployment,
database rebuild, private-state change, commit or push.
