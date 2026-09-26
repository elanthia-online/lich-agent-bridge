# Discovery ranking improvement

Authorized 2026-09-06. Status: ranking and archive-neutral corrections deployed;
maintainer accepted the current release on 2026-09-07. Remaining passage-selection
quality is deferred to [issue #7](https://github.com/elanthia-online/lich-agent-bridge/issues/7).
See the research workspace plan for the release decision and historical results.

## Outcome and scope

Keep focused source pages discoverable for multi-facet questions without making
catalogs disappear from shopping questions. Preserve the existing search/read
interface, six returned candidates, character scopes, provenance, source handle
ownership, model adapters, and all request/deadline/action gates.

The revision-change read contract was fixed and verified locally in subsequent
[revision handoff work](Research-Workspace-Plan.md#revision-handoff-fix-2026-09-06).
Ranking itself does not change that contract or establish live answer quality.
No embeddings, model reranker, vector store, extra model calls, new settings,
game actions, production restarts, or private fixtures are required.

## Implementation

1. Add red regression cases at the real `KnowledgeBase.open_research().search()`
   interface for broad catalog noise, title recall, shopping, history, diversity,
   stable ordering, and false substring matches.
2. For research only, combine bounded SQLite FTS lanes: twelve general matches,
   twelve title matches, and twelve non-catalog matches (including archives). Union by page
   identity before ranking. Existing one-hop redirect resolution remains bounded.
   This deliberately increases internal candidate work from twelve to at most
   thirty-six unique initial pages, not model-visible results or model calls.
   Legacy excerpt search retains its current retrieval behavior.
3. Put deterministic ranking in one pure module shared by discovery sources.
   Favor distinct title and heading matches over repeated body keywords. Use
   conservative title-based catalog/archive hints, adjusted for explicit shopping
   or history intent. Archive location alone does not reduce relevance for a
   mechanics question. These hints do not establish authority or currentness.
4. Rank first, then diversify editions of the same catalog family when
   alternatives exist. Keep deferred editions as backfill, not a blacklist.
   Include compact match reasons with candidates for diagnosis. Ranking must not
   alter passages or claim that an unread source establishes a fact.
5. Verify the selected mechanics section reaches a fake model's final prompt.
   Run existing source, privacy, budget, cancellation, integration, and full-suite
   checks. Read-only local-mirror probes may inform coverage; never copy private
   documents into public tests.

## Acceptance and limitations

- A mechanics page survives six keyword-rich shop pages; repetition alone cannot
  raise a source's relevance indefinitely.
- A relevant page beyond the general FTS shortlist is admitted through another
  lane. Final ranking cannot repair a missing candidate by itself.
- Shopping, explicit named catalog, and historical queries retain those sources.
- Near-identical dated editions do not displace independently relevant pages.
- Equal-score ordering is stable; lexical matching respects word boundaries.
- Returned handles, text, revisions, scopes, and budgets retain existing tests.
- No source-specific answer rules or special cases for the reported game item.

This remains lexical retrieval, not guaranteed semantic understanding. Bounded
shortlists can still miss pages, and title-based kind hints can be incomplete.
Live answer quality remains an open gate after offline ranking and revision/read
tests pass. The initial live comparison exposed archive suppression, detailed below.

## Verification (2026-09-06)

- New ranking suite: 12 synthetic tests. Seven initial cases failed before the
  implementation. An additional numbered-spell regression failed before the
  numbered-title preference was added.
- Added fake-model integration coverage: a focused rules page survives catalog
  noise, its selected section is read, and its attributed passage reaches the
  final prompt. No game operations are dispatched.
- Full Python suite: 470 tests passed using
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -t . -q`.
- MCP: 22 tests passed; `npm run typecheck` and `npm run check:sdk-types` passed.
- Read-only probes against an existing local mirror improved focused-page recall
  for a broad equipment query and retained shops for a shopping query. Four
  individual new searches took roughly 10–116 ms on this workstation; these
  one-off measurements are not a latency benchmark or a live-answer evaluation.
  No network fetches, model calls, game commands, or private-note inputs were used.

Implementation: new `discovery.py`, research candidate ordering/metadata, bounded
research-only FTS recall lanes in `knowledge.py`, and regression tests. Ranking
uses no special-case knowledge about the item from the reported failure.

At the initial ranking verification, the deployed runtime remained unchanged.
Subsequent revision handoff verification
passed 478 Python tests and 22 MCP tests, plus type and generated SDK checks.
The local fix supplies replacement read passages within the existing request
when source/section identity permits; it does not close the live quality gate.
No larger research budget or new action authority was added.

## Archive-neutral correction (2026-09-06)

The first deployed comparison confirmed revision handoff and conservative
character freshness, but missed combination rules recorded in a saved primary
post. A local-mirror probe recalled that source yet ranked it fourteenth for a
natural query; removing only its archive score adjustment moved it to third.
That probe is a controlled query, not a trace of the live model's exact search.

Three synthetic regressions at `open_research().search()` failed before fixing:

- A saved rule was demoted below incidental title matches and never read.
- An archived rule absent from the first two FTS lanes was excluded from the
  non-catalog recall lane.
- Distinct archived references sharing a parent were deferred as duplicate
  catalog editions.

The correction removes the ordinary-query archive penalty, admits archives to
the existing non-catalog lane, and limits edition diversification to catalogs.
It retains explicit history/shopping preferences, provenance, freshness,
source deduplication, and all candidate/output/request limits. It does not
promote archived text to truth or hardcode any game item or mechanic.

All 15 ranking tests pass. The original three-query local-mirror probe now
returns the needed source in all cases, at ranks 1, 4, and 1. Full-suite and
live comparison results are recorded in the research workspace plan.
