# Changelog

All notable changes to Turnstile are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/) with the pre-1.0 caveat
described in [STABILITY.md](STABILITY.md).

## [Unreleased]

## [0.1.4] - 2026-08-25

### Added

- **Stale-instance collapse and gc.** Active instances idle past
  `settings.stale_after_days` (default 14; 0 disables) are collapsed by
  the guard into a one-line inventory instead of full per-instance
  context blocks, ending the wall of months-old "STALE" warnings on
  every guarded edit in a long-lived checkout. Their permissions still
  apply — a deny attributed to a stale instance now recommends resuming
  or cleaning it up rather than "transition first" — and when every
  instance is stale the least-idle one still renders in full so the
  guidance keeps its post-compaction anchor. Waiting instances are
  exempt, as are suspended parents (whose `updated_at` freezes at
  suspension while the child advances). The new `turnstile gc`
  command / `process_gc` MCP tool abandons stale instances (archived
  under `.process-state/abandoned/`, never deleted) through
  `Engine.abandon`, so each collection emits an `abandon` event to the
  stream, re-checking each candidate immediately before acting so a
  concurrent transition rescues it; `--dry-run` / `dry_run` previews and
  `--older-than-days` (positive) overrides the threshold.
- **Per-instance work directory.** `process_start` accepts `cwd`
  (engine `start(cwd=...)`): the directory — typically a git worktree —
  where the instance's work happens. Gate validations and actions for
  that instance run there instead of the engine's project root, fixing
  gates that ran `git diff`/tests against the main checkout while the
  work lived in a worktree. Must be an existing absolute directory
  (rejected otherwise, so a typo cannot silently fall back to the wrong
  directory). Recorded on the instance as `project_dir`, inherited by
  subprocess/dispatch children, reported by `process_status` in both
  the by-id and list shapes, and falling back to the project root only
  if the recorded directory later disappears.
- **Agent designation on states.** `agent_context` gains `agent`,
  `model`, and `fresh_context`: a state can name the (custom) agent its
  work is designated for, optionally pinning a model and requiring
  fresh context. Advisory, like the rest of `agent_context` — surfaced
  in `process_transition`/`process_status` responses and as a line in
  the guard's guidance.

### Fixed

- **Unwritable log.txt no longer aborts state operations** — the log is
  fail-open at the store level; previously a read-only log file or full
  disk could raise after an operation had already persisted, reporting
  failure for work that landed. Instance JSON and the event stream are
  unaffected.
- A torn or hand-mangled instance state file now raises
  `CorruptInstanceError` naming the exact file, across load and archive
  listings, instead of a bare JSON error with no path.
- `turnstile enforce` commands handle a corrupt or malformed
  `.claude/settings.json` cleanly: an actionable error naming the file
  (never a traceback, never an overwrite), JSON nulls in the hooks
  structure normalized, `enforce status` degrading to
  "hook: unknown (reason)", and settings writes made atomic. The
  registry's enforcement mode now changes only after the hook install
  succeeds; previously a failed install could flip the effective mode
  and then report failure.

## [0.1.3] - 2026-07-23

### Fixed

- **Starter-pack gate variables now expand** — six validation gates across
  `feature-development`, `bug-fix`-adjacent starters (`scientific-method`,
  `spike`, `decision-record`, `peer-review`) wrapped `${var}` in shell
  single quotes, so the variable never expanded: the issue-linkage gate
  always reported "linked" and the skip-when-none / file-exists checks
  tested a literal `${output_file}`. All six use the env-var-safe
  double-quoted form now.
- `feature-development`'s `test` state and `bug-fix`'s `reproduce` state
  no longer forbid edits: test authoring happens in `test`, and writing a
  failing repro test is the point of `reproduce`.
- `test_command` parameter descriptions warn that the bare `pytest`
  default resolves whatever is first on PATH in the gate's environment,
  and show an explicit project-specific invocation.
- A regression test now runs the shipped starter gates through the real
  command runner and structurally sweeps every bundled command for the
  single-quoted-`${var}` anti-pattern; the sweep immediately caught a
  seventh instance (`Domain: ${domain}` display gates in
  `decision-record` and `scientific-method`), also fixed.

### Changed

- **Claude Code adapter extracted from core** — the CC-specific pieces
  (hook JSON parsing, response shapes, `.claude/settings.json`
  installation, pinned guard commands) moved from `turnstile_core.guard`
  to `turnstile_cli.claude_adapter`; `turnstile-core` no longer knows any
  runtime's hook formats. The agnostic contract every adapter consumes is
  `turnstile_core.enforcement.check_enforcement` / `EnforcementResult`.
  `update_registry_enforcement` (runtime-agnostic) moved to
  `turnstile_core.loader`. The `turnstile guard` entry point is
  unchanged. (#32)
- `Engine._enter_state` is the single canonical state-entry path for
  transitions and signals; the duplicated dispatch-on-signal block is
  gone. Zero behavioral change (equivalence-verified). Pre-existing
  signal-entry asymmetries are now tracked as #41 and pinned by tests.
  (#33)

## [0.1.2] - 2026-07-23

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
- **Static graph analysis at validate time** — `turnstile validate` and
  `process_validate_definition` now warn about unreachable states and
  dead-end sinks (states with no path to any terminal), and a dispatch
  state's `immediate` target is checked at load time instead of failing
  mid-process. The graph walk lives in `turnstile_core.graph` with a
  reusable API (edge map, reachability, terminal-reachability). (#30)

### Fixed

- Skip overrides record the acting session in `triggered_by`; previously the
  override log carried no actor attribution.
- Gate and action commands that emit non-UTF-8 bytes no longer raise
  `UnicodeDecodeError` out of the command runner; output is decoded
  lossily (`errors="replace"`).
- Generated configs pin the current release tag: `TURNSTILE_REF` was
  still `v0.1.0` in v0.1.1, so `turnstile init` on v0.1.1 produced
  configs one release behind the CLI that wrote them.

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
