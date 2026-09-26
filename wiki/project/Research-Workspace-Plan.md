# Search, read, and question-local research workspace

Approved architecture and implementation direction: 2026-09-06.
Status: implemented, deployed, and accepted by the maintainer for the current
release on 2026-09-07, with remaining multi-part passage-selection quality
explicitly deferred to [issue #7](https://github.com/elanthia-online/lich-agent-bridge/issues/7).
The latest live comparison remains incomplete; acceptance to ship is not a claim
that every requested mechanics facet is answered. Publication to main is
authorized. The dated sections below retain the investigation history.
Supersedes the search-only/append-only approach in Evidence-Gathering-Arc.md;
the evidence-budget patch remains the starting baseline, not a completed solution.

## Outcome

An answer model can discover sources, deliberately read relevant sections,
follow bounded continuations, and revisit evidence without accumulating an
irreversible prompt transcript. Relevant mechanics and character observations
remain distinct. All source text remains untrusted data.

## Scope fence

- Retain model adapters, identity/generation fences, cancellation, action broker,
  verified observation storage, configuration entry point, and private-data policy.
- No new game command authority, raw filesystem/SQL/URL tools, vector database,
  model-generated durable facts, GUI work, or Vellum changes.
- Retain the existing question deadline and three-round/eight-request ceiling.
  Any demonstrated need to expand these requires a separately disclosed decision.
- Public fixtures are synthetic. No production logs or character records enter git.
- No deployment, push, or merge until integration review and player testing gates.

## External research interface

The model sees `knowledge.search(query, scope?)` and
`knowledge.read(source_id, section_id?, cursor?)`. Scope defaults to `reference`;
optional `character` and `development` scopes isolate purpose. Source identifiers,
section identifiers and continuations are issued and validated by the question's
research session. Neither a path nor an arbitrary URL is accepted as a handle.

Search returns compact candidates with source_id, title, snippet, provenance,
and a section outline. Search snippets are discovery evidence, not proof a whole
source was read. Read returns a bounded, attributed passage, completeness and
continuation information. Search/read share configured local and online policy.
Known local documents can be read in sections; stale selected GSWiki pages can
be verified/refreshed by identity without repeating a broad discovery query.
When verification changes the revision, a whole-page read or a unique exact
structural section match returns the replacement passage in that same call.
Unmatched or ambiguous sections require deliberate replacement selection;
continuation offsets are never translated across revisions.
Unverified stale content must retain its stale/unknown-current label.

Result shape uses the existing status/data/sources/diagnostics envelope. Sources
describe material actually supplied, including section/range and revision where
available. Character scope remains bound to the admitted character.

## Work packages and ownership

1. Knowledge research module: source-handle registry, source selection, scoped
   discovery, full-source/section reading, bounded paging, configured adapters,
   freshness/provenance, and isolated fixture tests. Own knowledge.py and a new
   research.py plus corresponding tests; do not edit evidence_tools or loop.
2. Evidence workspace: replace append-only prompt admission with bounded
   question-local retained results and prompt selection. Prefer deliberate reads
   over discovery snippets; retain compact handles for omitted/evicted content;
   identical requests reactivate known evidence without repeating side effects.
   Deduplicate stable source/revision/range rather than complete search envelopes.
   Own evidence_loop.py, optional evidence_workspace.py, and corresponding tests.
3. Wiring and question context: advertise/validate research tools, create and
   close the character-bound research session, separate discovery from initial
   context, maintain legacy CLI/MCP search compatibility, and test the real path
   from research request through the final model prompt. Own evidence_tools.py,
   context_assembler.py, engine.py, and focused integration tests.
4. Controller: reconcile interfaces, review permissions/privacy/cancellation,
   run full tests, document actual behavior and limitations. Review findings may
   correct violations of this plan, not silently expand product scope.

## Acceptance tests

- A rule late in a long synthetic page can be selected/read and reaches the model.
- Multiple rule sections can be read without unrelated introductory text crowding them out.
- Later relevant reads displace early noisy matches; omitted text stays retrievable.
- Repeated reads reuse content without losing original provenance or repeating recon.
- Search snippets and complete/partial reads cannot be confused in source reporting.
- Scope excludes another character's notes even when stored outside a character folder.
- Invalid/foreign/expired handles, traversal strings, and injected instructions
  cannot change scope, issue arbitrary network requests, or dispatch game commands.
- A source changing revisions cannot silently mix passages from different versions.
- Actions-off, stale identity, cancellation, and question deadlines retain existing gates.
- Public Python suite, MCP compatibility checks as applicable, and deterministic
  fake-model integration tests pass before a player-authorized in-game comparison.

## Verification and rollback

Record commands, counts, and limitations in this document after integration.
Keep the currently deployed build untouched. The local feature commit can be
reviewed/reverted independently; an eventual deployment needs a fresh backup
and explicit health checks. Live answer quality is not proven by mock tests.

### Offline results (2026-09-06)

Working branch: `feat/research-workspace`. Implementation and documentation are
local working-tree changes, not yet committed, pushed, or merged. The subsequently
authorized private-runtime deployment passed 449 tests and service health checks;
its backup and local details are recorded only in the private operations wiki.

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -t . -q`:
  457 tests passed. Synthetic HTTP/socket tests ran with local socket permission;
  they did not contact the game or production services.
- In `mcp/`, `npm test`, `npm run typecheck`, and `npm run check:sdk-types`:
  22 tests passed; type and generated SDK checks passed.
- `python3 scripts/check-docs.py`: 49 Markdown files checked, zero broken local links.
- `git diff --check`: clean.

Independent review corrected cached observation reactivation priority,
multi-source recovery handles for evicted long-query searches, and oversized
manifest metadata at the minimum context allowance. Source tests also cover
cancellation during a fetch, source registry admission, revision replacement,
late headings, raw tables, and per-call completeness. HTTP integration tests
assert read passages and provenance reach the final model prompt, not merely
that a search request succeeded.

### Live gate diagnosis (2026-09-06)

The first multi-facet equipment question did not retrieve the necessary interaction
rules. Source reporting distinguished discovery from reads and the answer admitted
its gaps, but those safeguards do not make it a successful answer. Telemetry showed
three evidence rounds and eight tool calls in about 44 seconds against a 120-second
question deadline. No output-budget omission was reported in the supplied output.

Offline synthetic diagnostic replays reproduced two defects without model, game,
or network calls:

- Six broad catalog-like documents with repeated query terms can crowd a focused
  mechanics document out of all six discovery slots. Ranking adds title matches
  to capped per-term occurrence counts; broad coverage can outweigh the focused
  source. GSWiki retrieval also has a twelve-candidate FTS shortlist before this
  final admission step.
- A selected stale page changing revision returns a replacement handle/snippet,
  not a read passage, even after the new page was fetched. On the last permitted
  round no further deliberate read can occur. Changing only the synthetic live
  revision to match the mirror makes that replay supply a read successfully.

These replays demonstrate mechanisms, not a complete reconstruction of the live
model's exact request ordering. The existing tests checked refresh and explicit
follow-up reads separately, missing their interaction with the round ceiling.
Diagnosis made no production-code changes, restarts, or game commands. Any revised
read/refresh contract, ranking fix, or increase to bounded research allowances
was to be disclosed before implementation; live acceptance remains open.

The subsequently authorized [discovery-ranking work](Discovery-Ranking-Plan.md)
is implemented locally with 470 Python tests and 22 MCP tests passing. It is not
deployed. The subsequent revision handoff fix below also remains local; these
offline results do not close the overall live acceptance gate.

### Revision handoff fix (2026-09-06)

The changed-revision read defect is fixed locally in `research.py`. Before a
snapshot has supplied a passage, verification may replace it and fulfill the
requested read in the same call. Whole-page requests use the new page. A section
request follows only an exact structural path that is unique in both snapshots:
heading levels, ancestor headings, and whitespace-normalized, case-folded heading
text must match. Changed offsets alone do not prevent a match. A missing,
ambiguous, reparented, or level-changed section returns `revision_changed` with
the replacement candidate and no read evidence, allowing explicit selection.

Successful handoffs return the replacement's source/section handles, revision,
provenance, and bounded passage/continuation. `refreshed_from` identifies the
previous source and revision; a `refreshed_read` diagnostic records the handoff.
An old cursor is never reinterpreted against a replacement. Once a snapshot has
supplied read text, it remains pinned for the question, including when online
verification failed and the stale snapshot was read. Repeated requests through
the old source handle reuse the registered replacement without another fetch.

Eight synthetic regressions in `tests/test_research_revision.py` cover whole-page
and structural section handoff, safe refusal of uncertain section matches,
replacement continuations, pinned old snapshots, cancellation during refresh,
and heading-level changes. A fake-model integration case confirms refreshed
passages and their revision provenance reach the final prompt on the third
evidence round and eighth tool request, without game operations.

Verification after this fix: 478 Python tests and 22 MCP tests passed; type and
generated SDK checks passed. These counts precede subsequent CLI work. No
deployment, runtime restart, or push accompanied this fix. The three-round,
eight-request ceiling is unchanged, and live answer quality remains unverified.

### Remaining limits and live gate

- Selection uses tool class, recency, and explicit reactivation, not an automatic
  semantic relevance judge. The model must request the appropriate sections.
- General-web results remain discovery-only. Markdown ownership requires the
  documented filename/metadata convention; unknown free-prose identities cannot
  be inferred. Long table continuations may require earlier pages for context.
- The existing three rounds and eight requests still bound research. Passing
  synthetic tests does not establish live-model answer quality or latency.
- Before pushing: complete the player comparison of the same multi-facet
  equipment question with actions off on the backed-up, health-checked deployment. Inspect
  actual passages, omitted/recoverable results, provenance, and timing. Live
  recon testing remains separately subject to player authorization.

## Approved testing entry point (2026-09-06)

The player requested direct shell-to-LAB questions so the same corpus can be
tested against different player-logged-in characters without a copy/paste relay.
Implemented `labctl ask` and `labctl questions` over the existing authenticated
question path, not a separate answer engine. Corpora are bounded and sequential;
they preserve normal conversation, record private answers/sources/timing, and
stop on failure without retries. No login or account-switch automation was added.

Read-only is enforced per question at the server, independent of global action
settings. Explicit `--allow-recon` permits only existing gated INFO/SKILLS.
Fresh-state preflight and server-side generation admission bind tests to the
selected session. Details and corpus format are in
[Developer testing](Developer-Testing.md#direct-questions-and-private-question-corpora).

Controller verification: 500 Python tests, 22 MCP tests, type/generated-SDK
checks, and 50 Markdown files with zero broken links. This includes CLI through
an isolated authenticated HTTP server into the actual evidence/answer path,
read-only denial despite enabled actions, unchanged subsequent action policy,
session replacement, output privacy/no-clobber, and no-retry failure cases.
Subagent implementation and documentation outcomes were checked against current
source and those tests. No live game/model questions, deployment, restart,
commit, or push accompanied this addition. Live quality acceptance remains open.

## Archive-neutral follow-up

The initial live corpus completed without game commands and confirmed revision
handoff, but failed to retrieve known combination rules in an archived primary
post. Three synthetic regressions reproduced archive score suppression, recall
exclusion, and inappropriate grouping of distinct archived references. All three
failed before the correction and pass afterward; all 503 Python tests and the
documentation checks pass. The unchanged local-mirror probe now passes all three
queries through the production discovery interface.

The correction was deployed to the private runtime with a rollback backup;
both LAB services are healthy. It changes only discovery.py and knowledge.py,
not budgets, game permissions, profiles, or source freshness. The character feed
was stale at the comparison gate, so no follow-up live questions were run.
Resume the unchanged read-only corpus after the player confirms a fresh session.
No commit, push, or claim of full live quality acceptance accompanies this fix.

## Selected-page reuse and passage-selection investigation

The unchanged read-only corpus was repeated after the archive-neutral deployment.
The broad question now discovered the relevant archived primary post, but did not
read it. The targeted follow-up did read it and explained the previously missing
combination rules. Current personal training was correctly left unverified. No
game commands or aggregate evidence-budget omissions occurred. This is improved
but still partial answer-quality acceptance, not a release pass.

The follow-up also hit the local exact-page refresh limiter. A deterministic
shared-KnowledgeBase/two-ResearchSession regression reproduced revision rollback:
the first question read a verified replacement, while the next returned the old
stale mirror revision. Exact-page reads lacked the success cache used by search.
The correction reuses successful title-keyed page snapshots within the existing
TTL and entry bound, before the limiter, with original retrieval timestamps and
question-local handles. New/expired/failed reads retain existing policy and rate
limits. This correction is not yet deployed.

Passage selection remains under investigation. Scripted-model transport tests
prove that a selected late section can reach the answer, not that a live model
will select it. A private exact-turn trace is needed to distinguish missing
candidate detail from model choice; no additional public tracing feature,
budget increase, or game-specific retrieval exception has been implemented.

Controller closeout: reviewed the refresh-cache subagent's implementation against
the shared-source/two-question regression and verified all 513 Python tests,
including ten new cache regressions. Fifty Markdown files have no broken local
links; whitespace checks pass. Newer disk entries supersede older memory entries,
and unavailable persistence retains bounded in-memory reuse. The passage-selection
subagent found no exact-symptom synthetic reproduction and made no changes.
The proposed private model trace was blocked by the execution safety check pending
explicit private-payload approval. No deployment, restart, commit, or push was
performed. Live selection quality and post-deployment behavior remain unverified.

## Authorized selection trace and outline coverage

After explicit private-payload approval, one isolated configured-model question
ran against captured state with no game-action transport. Relevant sources and
section titles were present in discovery. The model spent its remaining requests
on reads including both a parent section and a child already contained in that
parent. The broad answer still omitted some requested rules. This establishes
redundant selection in that trace, not the model's reason for choosing it or proof
that request limits alone caused the incomplete answer.

The bounded correction exposes existing section start/end character offsets in
discovery and explains parent/child overlap in the evidence contract. Actual read
ranges and continuations still determine what evidence was supplied. Three new
synthetic regressions cover nested and unrelated sections, duplicate titles,
Unicode, partial reads, and truncated outlines within existing packing limits.
They failed for missing offsets before implementation. No request, round, size,
deadline, or game-action limits changed. Cache and outline corrections remain
undeployed; live answer-quality acceptance remains open.

Controller closeout: reviewed the passage-selection subagent's minimal metadata
change and synthetic red/green cases, then verified the complete package-aware
Python suite: 516 tests pass. The integration test confirms section ranges and
their interpretation reach the model contract and selected read. All 50 Markdown
files pass local-link checking; whitespace checks pass. An initial test command
used the wrong discovery root and failed relative imports; the corrected full
run passed without code changes. The private trace remained private and issued
no game commands. No deployment, restart, commit, or push occurred. The next
acceptance gate is the same broad-question comparison on an authorized deployment;
synthetic success is not proof of improved model selection.

## Deployment comparison outcome

After player authorization, the cache and outline changes were deployed with a
rollback archive. Both services passed health checks; 449 installed-runtime tests
and 27 focused public regressions against installed modules passed. Private
settings and gameplay policy were preserved.

One unchanged broad-question comparison took 53.670 seconds. Supplied reads no
longer overlapped, and selected pages were verified without limiter or aggregate
omission diagnostics. However, relevant combination rules remained unanswered:
the model read a relevant source but not the needed passage. Acceptance remains
partial. This single run does not prove cross-question cache reuse or isolate
the metadata change from model variation. The read-only action audit contained
no game-command proposals or executions. No additional scope, commit, or push.

## Release decision (2026-09-07)

The maintainer accepted the deployed behavior as good enough for now and requested
publication to main. Remaining passage-selection tuning is a non-blocking follow-up
in [issue #7](https://github.com/elanthia-online/lich-agent-bridge/issues/7), with
privacy-safe reproduction guidance, multi-topic coverage criteria, and the existing
scope/budget fence. This supersedes the earlier quality gate as a release blocker,
not its recorded findings. Do not close the issue merely because this work ships.
