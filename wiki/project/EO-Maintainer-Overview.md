# LAB: maintainer overview

LAB is an optional agent integration for Lich. It supplies observations,
knowledge retrieval and explicitly registered operations to CLI/MCP clients and
configured models. The frontend and ordinary Lich scripts remain independent.
The repository now lives at [elanthia-online/lich-agent-bridge](https://github.com/elanthia-online/lich-agent-bridge).
Repository ownership does not imply acceptance of the companion Lich or scripts changes.

## Repository responsibilities

| Project | Responsibility | Not responsible for |
| --- | --- | --- |
| Lich | Optional cooperative execution guards, exact script lifecycle, native observations and static routing | LAB policy, model selection, character-specific tests |
| EO scripts | Bigshot Quick encounter behavior and scoped eLoot equipment/room cleanup | Agent inference, credentials, knowledge retrieval |
| LAB | Ruby bridge, Python service, model/knowledge adapters, CLI/MCP, operation approval and results | Replacing login/frontends or granting arbitrary script execution |

Keeping LAB as a standalone repository preserves its Python/TypeScript/Ruby
dependency set, configuration, tests, documentation and release process.
Distributing its Lich-facing entry scripts through EO scripts could be considered
separately, with explicit compatible-version/install handling. Moving only those
files does not move the service they require. Its separate repository is not an
architectural prerequisite for the companion changes.

## Why touch casting and waits?

The Lich change does not remove normal `Spell.cast` retries or introduce a global
"never retry" flag. Without an installed execution guard, ordinary behavior is
retained. Within an explicitly guarded scope, each attempted wire command passes
the caller's policy; cooperative waits check cancellation and the same deadline.
A test can therefore bound sends or reject a retry without rewriting casting.

One wire-send budget is not necessarily one cast attempt: preparation, stance,
casting and helper commands may be separate sends. A precise test policy must
distinguish those commands. An accepted write is not proof of a successful cast.
Hindrance, roundtime and game outcomes remain observations, not success inferred
from the absence of an exception.

The guards are cooperative, not a Ruby sandbox. Direct socket access, arbitrary
uncheckpointed loops and unrelated scripts are outside their coverage. Native
Lich must not depend on LAB to use these APIs.

## Current testing slice

One supervised outing starts at an explicit player-designated refuge, performs
bounded Quick work in a reviewed profile area, settles equipment and returns.
The controller performs time-sensitive work locally; model latency is not part
of combat response or recovery. Ordinary stop requests return. Hard revocation
denies all further sends, including travel, and can leave recovery incomplete.

Results separate work, actual observed effects, equipment recovery, refuge
arrival and exact-owner release. Unverified recovery blocks another test.
Offline suites pass. Player-authorized safe-refuge acceptance on 2026-09-10
verified both ordinary stop and a bounded seek/combat/return outing. The latter
traversed 28 rooms each way, produced four game-confirmed kills, restored the
original held item, released all owners, and ended without alerts. This is one
low-risk lifecycle acceptance, not a general claim about arbitrary routes,
profiles, creatures, or loot behavior.

Reviewed weapon/creature case batches are a subsequent slice, not a shipped
automated campaign. No automatic login, arbitrary equipment swapping or model-
directed emergency actions are added here. Public installs register no combat
capabilities; private characters, routines and logs stay outside the repository.

See [Architecture](Architecture.md), [Safety](Safety.md),
[Developer testing](Developer-Testing.md), and
[Safe-refuge acceptance](Agent-Test-Safe-Refuge.md).
