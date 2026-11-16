# Configuration and knowledge

LAB uses one validated settings interface, one knowledge interface, and multiple
model adapters. Setup and diagnosis share the same resolved configuration as
the running services.

## Settings

`labctl setup` writes a validated TOML file and asks before replacing an
existing one. `labctl config path` identifies it; `labctl config show` gives
a redacted resolved view. `labctl doctor` inspects local prerequisites and
service readiness without issuing game commands.

The default config path follows XDG conventions under
`~/.config/lich-agent-bridge/`. Explicit configuration and supported environment
overrides remain available; consult CLI help and the settings schema for exact
precedence rather than maintaining a separate undocumented settings layer.

Named agent profiles select provider, model, effort where supported, timeout,
and one optional bounded UTF-8 instructions file. Configuration stores
credential environment-variable names, not secret values. LAB's safety and
evidence contract remain authoritative over custom instructions.

## Model adapters

- Codex CLI uses an existing local CLI installation and authentication.
- Claude Code CLI uses an existing local CLI installation and Claude
  subscription login, never an inherited Anthropic API key.
- OpenAI Responses and Anthropic Messages use explicitly configured API
  authentication.
- The compatible Chat Completions adapter supports local servers such as
  llama.cpp without assuming they implement the Responses API.

A local transport endpoint and a cloud answer backend are separate privacy
choices. Backend prompts may contain selected private game observations.

## Knowledge

Configure an optional private Markdown root and optional local GSWiki SQLite
mirror. Live GSWiki fallback applies according to policy when local evidence is
missing, stale, or insufficient. General web is a separate opt-in provider with
its own credential environment variable.

`labctl wiki status` reports mirror state and freshness.
`labctl wiki refresh` prepares a replacement and preserves the previous usable
mirror when refresh fails. Downloaded mirrors and community-script corpora are
local cache data, not source-distribution assets.

Knowledge results carry source, revision where available, and timing metadata.
Tests must verify relevant evidence reaches the rendered prompt; availability
of a database file alone does not prove useful retrieval.

GUI setup, automatic wiki editing, and persistent model dialogue are outside
this configuration interface. See [Setup](Setup-and-Operations.md).
