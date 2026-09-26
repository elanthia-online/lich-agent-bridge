# Architecture

LAB complements Lich and the user's frontend. It does not replace their login,
game protocol, or established scripts.

## Ownership

```text
Game ↔ Lich ↔ frontend
         ↕
  LAB Ruby bridge ↔ Python SessionHub ↔ model adapter
                           ↕
                     MCP / labctl
```

The Ruby bridge extracts native Lich state, intercepts private questions,
displays answers, and executes independently validated broker instructions.
The Python service reduces observations, assembles context, manages knowledge,
and coordinates questions and registered operations. MCP and CLI clients use
the same SessionHub contracts. The frontend remains a presentation client.

## Lich boundary

`lab.lic` is a short-lived dispatcher. `lab-bridge.lic` owns the long-lived
hooks and workers; `lich-state-core.rb` provides frontend-neutral state.
Each runtime dependency must be installed in the active Lich scripts directory.

The bridge's command allowlist is independent of the server's policy. Immediately
before execution it checks character, session generation, room, expiry, and the
local execution switch. A disclosed utility sequence is one bounded action,
not permission to send an arbitrary command chain.

## State and knowledge

WorldState admits identity-bound snapshots and publishes bounded meaningful
events. The context assembler combines recent observations, current-state
provenance, optional private notes, inventory dossiers, and reference excerpts.
Missing data and budget omissions remain unknown.

Inventory and character observations persist in a user-selected private
database. These records are evidence with dates, not current-state guarantees.
Model text is not automatically written into durable knowledge.

Knowledge adapters support scoped discovery and deliberate reading across
Markdown notes, local GSWiki SQLite, and configured live GSWiki. A question-owned
research session resolves opaque source/section/continuation handles; a separate
evidence workspace retains bounded results and selects each turn's context.
Legacy search remains available. The model does not receive filesystem, SQL, or
unrestricted URL access; general-web fallback remains discovery-only.

Research candidate recall stays inside the knowledge adapter. The pure discovery
ranker orders scoped snapshots across sources without I/O or additional model
calls; source handles, section reads, and final evidence selection retain their
own existing responsibilities.

## Questions

One active question is admitted per character, with a global limit of four.
A single end-to-end deadline includes context assembly, model calls, and evidence
work. Busy requests fail promptly. An uncooperative worker retains its slot
until it actually exits, preventing orphan work from bypassing the bound.

The answer model may answer immediately or request only the advertised
[evidence tools](Evidence-Gathering-Arc.md). A provider-neutral protocol applies
to Codex CLI, OpenAI Responses, and compatible local-model adapters.

Forget and session replacement invalidate pending answers and temporary
dialogue. Synchronous cancellation revokes only the question's owned pending
recon operation; already-dispatched commands cannot be unsent.

## Operations

ActionBroker owns proposal, approval, dispatch, and completion state.
CapabilityRunner owns a registered operation's admission, bound identity,
deadline, postconditions, evidence, and terminal result. Transport success and
command dispatch alone do not prove a game outcome.

This distribution includes no personal combat strategies, hunting profiles, or
character-specific controllers. The controller registry is an extension seam,
not a bundled automation policy. Discover runtime capabilities and inspect
their schemas before use; discovery itself grants no authority.

## Presentation and deployment

LAB normally listens on loopback. The optional frontend-state adapter can
publish presentation data without coupling the bridge to a specific frontend.
Cloud model backends receive selected game context and reference text; loopback
transport does not make cloud inference local.

See [Safety](Safety.md), [Protocol](Protocol.md), and
[Setup and operations](Setup-and-Operations.md).
