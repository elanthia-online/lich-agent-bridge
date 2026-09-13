# Lich Agent Bridge documentation

LAB connects Lich sessions to agents through structured observations, bounded
knowledge retrieval, and independently gated operations. These public docs
describe the software, not a player's characters or installed configuration.

## Read by task

| Task | Reference |
| --- | --- |
| Install or configure LAB | [Setup and operations](project/Setup-and-Operations.md) |
| Understand component boundaries | [Architecture](project/Architecture.md) |
| Change command execution or run a live test | [Safety](project/Safety.md), then [Developer testing](project/Developer-Testing.md) |
| Read recorded combat trial evidence | [Combat reporting](project/Combat-Reporting-Plan.md) |
| Change context, retrieval, or evidence tools | [Context system](project/Context-System.md), then [Evidence gathering](project/Evidence-Gathering-Arc.md) |
| Change a Lich API, XML, or frontend integration | [Lich authoring references](lich/Lich-Authoring-References.md) |
| Work on an external script | [Script reference index](lich/scripts/README.md) |
| Inspect transport contracts | [Protocol](project/Protocol.md) and [Frontend state](project/Frontend-State-Protocol.md) |

[Project index](project/README.md) · [Lich index](lich/README.md) ·
[Game references](gsiv/README.md) · [Sources](reference/README.md)

## Private knowledge

Character notes, builds, equipment, routines, logs, and databases belong in a
separate user-controlled private location. Point the optional knowledge
configuration at that location; this checkout ships no character profiles.

Label recorded facts with their source and observation time. Live observations,
historical records, and model inferences have different authority. A historical
record is not proof of a character's current build or inventory.
