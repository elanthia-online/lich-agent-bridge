# Context system

For the optional symbolic System One evaluator (separate from this research
pipeline), see [Jev shadow decisions](Jev-Decisions.md). It provides bounded
decision-specific observations and measured outcome history without an execution
interface.

The context assembler provides one bounded interface for building a question
packet. Callers should not coordinate retrieval, privacy filters, token budgets,
or provenance themselves.

## Evidence layers

1. LAB's identity, safety policy, and evidence-tool contract.
2. Current player intent and bounded temporary dialogue.
3. Structured state with character, generation, timestamp, and freshness.
4. Optional historical character/item knowledge and reference excerpts.
5. Recent game text, explicitly treated as untrusted observations.

Observed state describes when it was captured, not necessarily the world when
a slow answer arrives. Preserve unknown, zero, false, inactive, and omitted as
different states. A recorded profile cannot overrule newer observed values.

## Retrieval

Model-directed questions start without automatic broad wiki excerpts. Their
question-owned research session supports compact scoped discovery followed by
deliberate source/section reads. A bounded evidence workspace rebuilds the working
context each turn, retaining recovery handles for displaced results. Legacy search
callers retain the existing excerpt interface described below; see
[research semantics](Evidence-Gathering-Arc.md#research-sources) for scope ownership,
freshness, and paging limitations.

Discovery outlines expose each section's half-open character range in its source
revision. Parent ranges include descendant sections, making overlapping choices
visible before reading. These are full-section ranges, not evidence already
supplied: bounded reads report their actual range, completeness, and continuation
cursor. Offsets must not be compared across sources or revisions. This metadata
helps selection but does not guarantee that a model avoids redundant reads.

When verifying a selected stale GSWiki source finds a new revision, a whole-page
read or a uniquely matching structural section read supplies the replacement
passage in that same call. Returned handles and provenance identify the new
revision; `refreshed_from` records the prior snapshot. Missing or ambiguous section
paths require explicit replacement selection. Continuations never cross revisions,
and snapshots already being read remain pinned for the question.

Successful exact-page verification uses the shared live-source cache, before
checking the network rate limiter. Later questions can reuse that page within
the existing cache lifetime, retaining its original retrieval timestamp and
receiving their own independent handles. New or expired pages still pass the
limiter; failed refreshes remain explicitly stale. This does not make cached
evidence current indefinitely or remove the configured online-policy gate.

The Markdown adapter reads only the configured root. Selected-character pages
and associated topic pages may be used from a private knowledge directory;
other characters' notes are excluded. Gameplay questions avoid project
implementation plans, while development questions can retrieve technical docs.
Curated gameplay passages must match a topic heading or multiple distinct query
terms rather than qualify through one incidental body word. Single-term
lookups and selected-character build records retain their existing routing.
Follow-up prompt labels are not treated as search topics.

Explicit spell questions prioritize canonical spell evidence over incidental
mentions. Current-spell queries exclude deprecated pages and unresolved
redirects unless history is requested. Relevant recorded skill summaries retain
their date and historical provenance.

The GSWiki adapter searches an optional local SQLite FTS mirror. Canonical URL
and revision deduplication occurs before budgeting. Sufficient local evidence
does not trigger redundant web work; missing or stale evidence can use configured
fallback. Source status distinguishes success, disabled, unavailable, and
not-needed. A fixture that merely proves a lookup occurred does not establish
that the right passage reached the model.

Legacy research discovery uses three bounded FTS lanes (general, title, and non-catalog
matches, including saved posts), twelve rows each, deduplicated before existing one-hop
redirect resolution. A pure ranker then prioritizes distinct title/heading matches,
numbered spell titles, and query intent over repeated body keywords. Catalog and
archive title hints are adjusted for shopping/history questions, but archive
location alone does not penalize a source for ordinary mechanics questions.
Only catalog editions are diversified with backfill; distinct archived references
are not presumed duplicates. At most six candidates still
reach the model, subject to the existing output allowance. Legacy excerpt-search
callers keep their prior retrieval path.

Each candidate's `ranking` field explains lexical match counts, intent, and a
kind hint. These are relevance heuristics, not authority or truth scores. They
cannot infer unknown terminology or guarantee all relevant pages survived the
shortlist. See the [ranking plan](Discovery-Ranking-Plan.md) for tests and limits.

When a valid derived passage index exists, research instead shortlists bounded
passage/title/non-catalog matches before loading at most six selected normalized
documents. Discovery labels matched ranges `kind=passage` and structural sections
`kind=section`; both use existing opaque section handles and `knowledge.read`.
Ranges are bound to revision, normalizer version and text fingerprints. An
unstarted passage read whose page changes requires selection from replacement
information, never a guessed offset or heading remap. Started reads remain pinned.
Missing/outdated/unusable indexes fall back read-only to page discovery.
Explicit offline indexing and staged sync are the only builders; indexing does
not renew source freshness. See the [PIF plan](Passage-Index-Plan.md).

For an already-selected indexed page, research can recover up to three relevant
indexed ranges using the full question's lexical terms. This reads only that
page's range metadata and scores its already-loaded normalized snapshot, with
explicit limits of 4,096 ranges and one million characters. Snapshot mismatch or
overflow takes the existing explicit legacy fallback; it never silently samples
an oversized page. This cannot recover a page absent from discovery.

Complementary nearby ranges can form one contiguous, read-sized window. All text
between them remains present, including table qualifiers; the window keeps real
range endpoints, common heading ancestry, and the original revision identity.
This improves use of existing read capacity without increasing budgets. Distinct
query-word coverage is a selection heuristic, not proof every question part is
answered. Ordinary section handles remain available. See the
[coverage expansion](Retrieval-Coverage-Expansion.md) for measurements and limits.

Indexed SQL discovery uses FTS-native rank ordering before loading its bounded
joined results. When those results already satisfy independent non-catalog and
per-source range limits, a redundant non-catalog query is skipped; otherwise it
still backfills separately. Namespace and deprecated-source filters precede the
limit. Equal-score cutoff follows FTS ordering, not an additional global title sort.

Within each source's admitted passages, distinct heading-path matches to the
query take priority; page-title terms are excluded from this within-page score
because they identify the source rather than a specific section. BM25 breaks
ties, retaining body-match ordering when no distinguishing heading matches.
This only reorders existing ranges in that source's slots: it adds no SQL,
documents, read allowance or source authority. It cannot recover passages that
missed the bounded shortlist or resolve vocabulary mismatches semantically.

Research packaging reserves each admitted source's best readable handle before
spending space on additional outlines. Further handles are allocated across
sources, with a structural-section route retained when space permits. The first
passage's duplicate preview may be omitted because its source snippet already
previews that range. Tight budgets can shorten previews with visible ellipses;
provenance, revision identity and read ranges are never shortened. Missing outline
entries remain explicit through `outline_complete=false`, and omitted candidates
through partial diagnostics. This does not guarantee six sources fit arbitrary
metadata, nor that a fixed reader chooses every needed passage.

## Optional semantic source ranking

Optional [semantic source reranking](Semantic-Reranking.md) runs after the bounded
indexed shortlist. It reorders only indexed reference candidates, protects whole
named references, and leaves curated slots and all reading/freshness/authority
rules intact. No optional model is loaded while disabled. Configured failures or
resource overflow retain lexical order with diagnostics; success is a relevance
hint, not verified mechanics. Selected-page passage recovery remains lexical.

## Character and item knowledge

INFO/SKILLS observations carry source, completion, observation time, generation,
and level context. Only validated observations update the private character
database. Infomon cache values without verified observation provenance do not
establish freshness.

Inventory dossiers retain exact observed object IDs, descriptive fingerprints,
facts, locations, and provenance. IDs can change, and recorded location is not
proof that an item remains accessible. Queries inspect recorded knowledge;
they do not silently scan live containers.

## Dialogue and cancellation

Temporary per-character dialogue is bounded and cleared by sidecar restart,
session-generation replacement, or `;lab forget`. It helps resolve follow-up
questions without making prior model answers authoritative.

Question cancellation suppresses the result and revokes owned pending recon.
A blocked worker retains its concurrency slot until it actually exits.
Durable inventory and character records are not deleted by forgetting a question.

## Diagnostics

`;lab sources` lists references in the final answer model's selected evidence,
distinguishing discovery snippets from read passages, and
retrieval status. `;lab context` reports category/dialogue availability.
Timing diagnostics distinguish context, model, evidence, and total latency
without logging raw private prompts.

See [Evidence gathering](Evidence-Gathering-Arc.md).
