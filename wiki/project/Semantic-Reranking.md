# Optional local semantic reranking

LAB defaults to lexical passage-index-first (PIF) retrieval. Semantic mode is an
opt-in local relevance step, not another agent backend, vector server, or source
of truth. It uses the same `knowledge.search` / `knowledge.read` interface.

## Enable deliberately

From the source checkout, inside the service's Python environment:

```sh
python -m pip install -e '.[semantic]'
```

Download the following two artifacts yourself into a private model directory.
LAB never downloads models or installs dependencies during setup or questions.
Use the pinned revision, not the moving model branch:

- [MiniLM ONNX weights](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/onnx/model.onnx), saved as `model.onnx`.
- [MiniLM tokenizer](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/tokenizer.json), saved as `tokenizer.json`.

Expected SHA-256 values:

```text
model.onnx      6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452
tokenizer.json be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037
```

Set the one optional field in the existing settings file:

```toml
[knowledge]
# Keep your other knowledge settings.
semantic_model_directory = "~/.local/share/lich-agent-bridge/models/all-MiniLM-L6-v2"
```

Alternatively, choose the directory during `labctl setup`. `labctl doctor`
checks artifact hashes and dependency availability without loading the model or
performing inference. A ready result is not a guarantee of inference success.
Restart the sidecar deliberately to apply changes. Remove the setting to return
to default lexical behavior. A derived index is required for semantic ranking;
use `labctl wiki status` and the documented offline `labctl wiki index` workflow.

## Boundaries and fallback

Only the existing, at-most-36 admitted passage hits are scored. Their complete
indexed ranges, titles, and headings are encoded locally; no new page is recalled
and no private character/curated source is sent to the encoder. Only eligible
reference slots are reordered. Whole-title phrase matches and numbered spell
references named in the question remain lexical anchors. This generic guard
prevents broad guides from displacing an explicitly requested reference.

The six-source limit, page-focused lexical range recovery, contiguous coverage
windows, source freshness, exact snapshot checks, read budgets, and game-action
permissions are unchanged. A high similarity score is not evidence of authority
or current mechanics. Missing recall and paragraph-selection failures remain
possible; the encoder cannot invent absent rules.

Each search considers at most 256 passage windows and 200,000 input characters,
including cached windows and title/heading text. Query text must fit 254 tokens.
The model uses 256-token windows with 32-token overlap and a title/heading prefix
of at most 64 tokens. All body tokens are covered; oversized inputs fall back as
a whole rather than scoring silently truncated passages. Existing per-question
request/deadline limits still apply; these are per-search resource caps.

The encoder loads lazily, uses two CPU inference threads, and admits only one
scoring call at a time. Concurrent calls use lexical order rather than waiting
in an inference queue. Cancellation is checked before and after model setup and
each bounded batch; an individual native inference call is not preemptible.

A process-local LRU retains up to 4,096 window vectors (about 6 MiB of float
payload, excluding model/runtime memory). Cache identity includes the pinned
model, source/revision/range identity, headings, and text. It keeps no raw query
history or persistent vector database. Restart discards the cache. Model/runtime
memory is substantially larger than the vector payload; the earlier full-wiki
experiment peaked near 816 MiB for its complete benchmark process.

Missing/invalid artifacts, unavailable optional dependencies, inference errors,
busy inference, changed ranking inputs, and resource overflows retain lexical
order and emit `semantic_reranking` diagnostics. Success reports encoded/cached
window counts and timing metrics. Configured mode does not imply every search
used semantics. No model, database, private corpus, or real query report belongs
in the public checkout.

## Verification

Synthetic tests cover exact-name protection, scope isolation, unchanged read
handles, read-only input loading, explicit fallback, cache invalidation/eviction,
full windows, overflow, busy inference, and cancellation. The runtime benchmark
uses the production provider without ranking hooks:

```sh
python scripts/benchmark-runtime-semantic.py \
  --questions /PRIVATE/PATH/questions.json \
  --database /PRIVATE/PATH/copied-indexed-mirror.sqlite3 \
  --model-directory /PRIVATE/PATH/model \
  --output /PRIVATE/PATH/runtime-results.json
```

It uses the same literal fact checks, three-read strategy, and 6,000/10,500
character allowances as the frozen earlier comparison. Networking is blocked.
The first pass starts with an empty runtime vector cache; the second reuses it.
Neither is an OS-cold disk benchmark. Case-level regressions and fallback counts
must accompany aggregate coverage. These scores measure supplied read evidence,
not agent answer accuracy. See [earlier experiments](Retrieval-Coverage-Expansion.md)
and the [combined implementation plan](Passage-Index-Plan.md).

### Actual-runtime results, 2026-09-07

The frozen corpus SHA-256 is
`a7e76f7612857f876951077b97e58d5f83029d7c6e01891a15c0ec3b6a1feb02`.
An isolated indexed copy contained 33,107 documents and 190,227 passages. The
production source files were hashed before and checked unchanged after the run.
The benchmark artifact SHA-256 is
`eec6311cb2e43e39247ae4a62843a2efe7239365973ecfd0b93a723cc1af528f`;
no raw wiki text, personal configuration, or real question report is published.

| Read allowance | Lexical facts | Semantic facts | Fully covered questions, lexical → semantic | Improved / regressed questions |
| --- | --- | --- | --- | --- |
| 6,000 chars | 74/119 | 86/119 | 26/48 → 32/48 | 7 / 0 |
| 10,500 chars | 74/119 | 88/119 | 26/48 → 33/48 | 8 / 0 |

Both first and warm passes gave these scores. All 48 semantic calls per pass
succeeded, without fallback. At 10,500 characters, source recall rose from
34/50 to 44/50. The explicit-name guard retained the named equipment reference
that the earlier unguided semantic experiment displaced. No query-specific
exceptions or changes to gold facts were introduced.

At 10,500 characters, semantic search-plus-read median/p95 were 1,761/2,711 ms
on the first pass and 158/841 ms on the warm pass. Lexical warm median/p95 were
165/1,084 ms. This fixed-order single run is not evidence of a semantic speedup:
warm timings vary with filesystem/runtime caches, and the first pass includes
encoding uncached ranges. These are retrieval times, not end-to-end agent latency.
No live game test or installed-data migration was performed.

Final release checks subsequently added non-regular model-file rejection and
explicit offline rebuild repair for corrupted derived rows. Neither changes the
measured source ranking, encoder mathematics, read selection, or input corpus.
