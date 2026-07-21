# Changelog

All notable changes to Turnstile are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/) with the pre-1.0 caveat
described in [STABILITY.md](STABILITY.md).

## [Unreleased]

## [0.1.0] - 2026-07-21

Initial public release.

### Added

- **Process engine** — deterministic state machine over YAML process
  definitions: `initial`/`normal`/`terminal` states, transitions, validation
  gates that run shell commands and block on failure, parameter substitution,
  and skip/abandon/undo semantics.
- **MCP server** (FastMCP, stdio) exposing 19 tools for starting,
  transitioning, signalling, dispatching, and inspecting instances.
- **CLI** (`turnstile`) for init, validate, list, status, graph, dry-run,
  schema export, CI `check-completed`, git hooks, and enforcement management.
- **Layer 2 primitives** (experimental): `role` and `agent_context` on states,
  `wait` states with signals, async `dispatch` with child instances, and
  session tracking on history.
- **Enforcement** — `off`/`monitor`/`enforce` modes wired into Claude Code via
  a PreToolUse guard that gates the Edit and Write tools.
- **Starter pack** — 11 curated process definitions installed by
  `turnstile init`, covering general-purpose (peer-review, decision-record,
  scientific-method, security-review) and developer (feature-development,
  bug-fix, code-review, release, spike) workflows, plus meta processes.
- **Process inheritance** — `extends`/`overrides` for reusing and specializing
  definitions.

### Security

- Gate and hook commands receive parameter, signal, and notification values
  through the environment rather than string interpolation, so a supplied
  value cannot inject shell syntax into a command. Gate environments are
  restricted to declared parameter names.
- Concurrent process instances resolve most-restrictive-wins: a permissive
  instance cannot lift another instance's edit restriction.
- A corrupt or unreadable state, registry, or definition file is treated as
  unknown enforcement state (blocks in `enforce`, warns in `monitor`) instead of
  silently disabling enforcement; an active instance whose definition cannot be
  resolved no longer defaults to permissive.
- Signal-driven transitions run the same validation gates as ordinary
  transitions.

See [SECURITY.md](SECURITY.md) for the trust boundary and how to report issues.

[Unreleased]: https://github.com/PaperArmada/turnstile/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/PaperArmada/turnstile/releases/tag/v0.1.0
