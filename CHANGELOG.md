# Changelog

All notable changes to Turnstile are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/) with the pre-1.0 caveat
described in [STABILITY.md](STABILITY.md).

## [Unreleased]

### Added

- **Typed event stream** (`.process-state/events.jsonl`) — every state
  mutation appends a typed event (started, transition, dispatch,
  subprocess_started, signal_received, skip, undo, abandon, complete,
  handoff, parent_resumed) with per-instance sequence numbers and session
  attribution. Append-only shadow record: instance JSON remains the source
  of truth; the stream's consumer contract is documented in
  docs/reference.md. Replaces nothing yet — it is the seed of the
  event-sourced substrate (#24) and validates fold-reconstruction ahead of
  that flip. (#29)
- `start`, `abandon`, `undo`, and `handoff` accept a `session_id` for audit
  attribution; the MCP server passes its session automatically.
- `turnstile history <instance_id>` — render the recorded trajectory of an
  instance (active or archived): every transition with actor attribution
  (triggered_by, role, session), validation gate results, structured
  metadata, and the override log. `--json` emits the full record. Rendered
  values are sanitized so recorded control characters cannot rewrite the
  terminal view under review.
- `process_history` (MCP) entries now include `role` and `session_id` when
  recorded.
- **Action failures are observable** — an `on_enter`/`on_exit` action that
  fails (non-zero exit, timeout, or execution error) emits an
  `action_failed` event to the stream (with stdout+stderr tail) and an
  `ACTION FAILED` line to log.txt. Actions still never block the state
  change. Previously every action failure was silently swallowed. (#31)

### Fixed

- Skip overrides record the acting session in `triggered_by`; previously the
  override log carried no actor attribution.
- Gate and action commands that emit non-UTF-8 bytes no longer raise
  `UnicodeDecodeError` out of the command runner; output is decoded
  lossily (`errors="replace"`).

## [0.1.1] - 2026-07-23

### Fixed

- `turnstile init` registers process definitions already present in
  `.processes/` (previously they were silently unstartable: a non-empty
  `registry.local` disables auto-discovery). Override files are registered
  when their `extends` target resolves; unparseable files and overrides with
  missing parents are reported and skipped instead of registered. Stems are
  YAML-quoted so unusual filenames cannot corrupt the registry. (#40)
- `turnstile init --enforce off` no longer writes a registry that fails to
  parse (a bare `off` was read as YAML boolean `False`). (#40)
- `dry-run`, `graph`, and `info` fail with a one-line error instead of a
  traceback for unresolvable process names, including a registration hint
  when the file exists unregistered and a pointer to the internal `name:`
  field when it differs from the filename. (#40)
- Re-running `turnstile init` (or switching enforcement modes) no longer
  removes non-turnstile hooks that share a PreToolUse group in
  `.claude/settings.json`; the merge filters individual hook entries instead
  of whole groups. Hooks lost to earlier merges must be re-added manually.
  (#39)
- State writes are atomic (same-directory temp file, fsync, rename): a crash
  or failed write can no longer leave a truncated state file; the previous
  good file survives. Instance-ID lookups match the exact ID instead of a
  substring, and an empty-string lookup raises instead of returning an
  arbitrary instance. (#28)

### Changed

- Instance IDs widened from 6 to 12 hex characters; state files with old
  short IDs still load. (#28)
- Quickstart rewritten around the verified cold-install path: install first,
  explicit registration step, enforcement heads-up, a CLI alias tip, and
  copy-paste prompts for delegating setup to an agent.

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
