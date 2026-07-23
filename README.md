# Turnstile

[![tests](https://github.com/PaperArmada/turnstile/actions/workflows/test.yml/badge.svg)](https://github.com/PaperArmada/turnstile/actions/workflows/test.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

AI agents skip steps. They declare work done before tests pass. They handle the same task differently in every session, because nothing carries forward the structural memory of "what state is this work in." When work spans multiple agents, multiple sessions, or compactions that erase the agent's context, the failure modes compound.

Turnstile is a process engine for AI agents. Process definitions are YAML files with states, roles, validation gates, and async coordination between actors. Multiple agents and humans cooperate on a single process instance, with structural consistency enforced across sessions.

Exposed as an MCP server, Turnstile is infrastructure your agents *use*, not a framework you build agents on. The engine sits between intent and execution: it rejects illegal transitions, provisions per-state context, and persists state to disk so work survives restarts, compactions, and operator handoff.

A CLI complements the MCP surface for validation, CI integration, and administration. Works with Claude Code or any MCP-compatible client.

## Demo

![Turnstile demo: peer-review with role handoffs and an illegal-transition rejection](docs/assets/demo.gif)

Run it locally (from a clone of this repo):

```bash
uv run --package turnstile-core python scripts/demo.py
```

## How it works

1. Define a process as a YAML file describing states, transitions, and validation gates.
2. An agent (or human) starts an instance of that process.
3. The engine tracks the current state and only allows legal transitions.
4. Validation gates run shell commands and check their output before allowing transitions.
5. Everything is logged: transitions, overrides, abandoned processes.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

In your project directory:

```bash
uvx --python 3.12 --from "turnstile-cli @ git+https://github.com/PaperArmada/turnstile.git@v0.1.1#subdirectory=packages/turnstile-cli" turnstile init
```

This generates the MCP server config, registry, Claude Code guard hook, and gitignore entries; process definitions already in `.processes/` are registered automatically. Enforcement starts in monitor mode (warnings only; `turnstile enforce off` disables it). Restart Claude Code and the process tools are available.

The generated config pins to a release tag (`@v0.1.1`), so installs are reproducible. To upgrade, bump the tag in `.mcp.json` and `.claude/settings.json` and run `uvx --refresh`, then refresh the starter definitions with `turnstile update --apply`.

For the full walkthrough (writing a process, driving it from an agent, and skipping a state), see [docs/quickstart.md](docs/quickstart.md).

## What's included

- **19 MCP tools** for starting, transitioning, signalling, dispatching, and inspecting process instances.
- **A CLI** for validation, dry-run simulation, Mermaid diagrams, JSON Schema export, CI integration, git hooks, and enforcement management.
- **Built-in starter processes** including general-purpose (`peer-review`, `decision-record`, `scientific-method`) and developer-oriented (`feature-development`, `bug-fix`, `release`, `spike`, `code-review`).
- **Enforcement modes** (`off`, `monitor`, `enforce`) wired into Claude Code via PreToolUse hooks. `monitor` warns on edits made without an active process; `enforce` blocks Edit and Write when the current state forbids them. The hook gates the Edit/Write tools, not the contents of shell commands.
- **Process inheritance and shared packages** for reusing definitions across projects.

Full surface documented in [docs/reference.md](docs/reference.md).

## Stability

Turnstile is pre-1.0. The core engine (initial/normal/terminal states, validation gates, parameter substitution, the foundational MCP tools, the CLI, the persistence layout) is treated as stable. Layer 2 primitives (`wait`, `dispatch`, `role`, `agent_context`, signals, inheritance overrides) are experimental and may evolve before 1.0. Full details in [STABILITY.md](STABILITY.md).

## Documentation

- [Quick start](docs/quickstart.md) — write a process, set up the MCP server, drive it from an agent
- [Reference](docs/reference.md) — states, gates, parameters, MCP tools, CLI, registry, enforcement, starter pack
- [Stability and versioning](STABILITY.md) — what's frozen, what may evolve
- [Vision](docs/vision.md) — north star, identity, capability layers
- [Design principles](docs/principles/) — progressive disclosure, render don't record, single process single document, fail open
- [Security policy](SECURITY.md) — trust boundary and how to report a vulnerability
- [Changelog](CHANGELOG.md) — release history

## Dogfooding

Turnstile uses its own processes in [`.processes/`](.processes/). The starter pack was built using `create-process`. Enforcement runs in monitor mode against the engine's own development, so Claude Code receives a warning when editing files without an active process. These processes exercise the design principles documented in [`docs/principles/`](docs/principles/).

## Project structure

```
packages/
  turnstile-core/     # Models, loader, validator, persistence, engine,
                      # analytics, hooks, notifications, inheritance,
                      # registry, schema, template
  turnstile-mcp/      # MCP server (FastMCP, stdio transport)
  turnstile-cli/      # CLI (Click)
docs/
  quickstart.md       # Step-by-step setup
  reference.md        # Full reference (gates, tools, registry, enforcement)
  vision.md           # North star
  principles/         # Design principles
examples/
  *.yaml              # Feature-showcase process definitions
scripts/
  demo.py             # 60-second narrated walkthrough
```

## Running tests

```bash
uv run --group dev python -m pytest packages/turnstile-core/tests/ packages/turnstile-cli/tests/
```

## Contributing

Issues and PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, and how to propose process definitions.

## License

Apache 2.0 — see [LICENSE](LICENSE).
