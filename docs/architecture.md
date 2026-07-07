# Architecture

> The engine should be small and stable; definitions are where domain
> knowledge lives. — [Vision](vision.md)

This document describes how `turnstile-core` is built and, more
importantly, *why it is shaped this way*. The shape is the argument:
a process engine whose job is making agent behavior legible must
itself be legible.

## The organizing idea: functional core, imperative shell

Every interesting question the engine answers — *is this transition
legal? which gates guard it? what parameters does the child process
get? what happens when this instance completes?* — is a pure function
of three things: the process **definition**, the **instance** snapshot,
and the **request**. None of those questions require touching a disk,
spawning a shell, or knowing what time it is.

So the engine is split along exactly that line:

- The **kernel** decides. Pure functions, no I/O, no clock, no
  side effects. Fully testable with in-memory values.
- The **runtime** executes. It loads and saves instances, runs gate
  commands, spawns children, fires notifications, writes the audit
  log — and takes every semantic decision from the kernel.

This is what guarantees the property Turnstile sells: *structural
consistency*. Two paths into the same state (a normal transition and a
signal delivery, say) cannot drift apart in behavior, because there is
only one implementation of "what arriving in a state means."

## The layers

```
turnstile_core/
  errors.py        exception taxonomy
  templating.py    ${var} substitution + placeholder detection
  definition/      what a process IS
    expect.py        the expect-expression grammar (parse + compile)
    model.py         pydantic models: states, gates, parameters, registry
    inheritance.py   extends/overrides merging
    registry.py      shared-source resolution (path / git / package)
    loader.py        YAML discovery and loading
    schema.py        JSON Schema export
    analysis.py      Mermaid graphs, dry-run, diff, migration checks
  instance/        what a run IS
    model.py         ProcessInstance, HistoryEntry, OverrideEntry
    store.py         file-backed StateStore (.process-state/)
    analytics.py     aggregate stats over archived instances
  kernel/          pure transition semantics — the heart
    plan.py          plan_transition, plan_signal, legality,
                     child-parameter resolution, parent resumption,
                     summaries
  runtime/         the imperative shell
    engine.py        Engine facade; _apply_entry (arrival semantics)
    gates.py         async gate execution (shell commands + checkers)
    notifications.py lifecycle notification commands
  ops/             tooling around the engine
    enforcement.py   "is this edit allowed?" decisions
    guard.py         Claude Code PreToolUse adapter
    githooks.py      pre-push / pre-commit generation
    scaffold.py      shared-package scaffolding
```

The import direction is one-way, top to bottom of this list:

```
errors, templating  →  definition  →  instance  →  kernel  →  runtime  →  ops
```

A module may import from its own layer or any layer above it in this
ordering, never below. `definition/` knows nothing about instances.
The kernel knows both but performs no I/O. The runtime is the only
place where decisions meet side effects. `ops/` consumes everything
and is consumed by nothing — deleting it would not disturb the engine.

Frontends (`turnstile-cli`, `turnstile-mcp`) sit entirely outside and
depend on `turnstile-core`'s public surface: the `Engine` facade for
anything involving instances, and the `definition/` + `ops/` layers
for definition tooling and setup. Core depends on no frontend.

## What lives where (and the rule for deciding)

**If it answers "what should happen?" it belongs in the kernel.**
Transition legality, required-metadata enforcement, signal validation,
`parameter_map` resolution, subprocess routing on child completion,
completion summaries. `kernel/plan.py` raises typed errors
(`TransitionError`, `SubprocessError`) for illegal requests and returns
plans for legal ones.

**If it makes something happen, it belongs in the runtime.**
`Engine.transition()` and `Engine.receive_signal()` both follow the
same shape: *plan (kernel) → run gates → record history → apply
arrival*. Arrival semantics — dispatch spawns a child and continues,
wait blocks on a signal, subprocess suspends the parent, terminal
completes/resumes/notifies — are implemented once, in
`Engine._apply_entry()`, and shared by every path into a state.

**If it describes the YAML artifact, it belongs in `definition/`.**
Including everything you can compute from a definition alone: the
expect grammar, Mermaid rendering, dry-run simulation, diffs,
inheritance. This is why `analysis.py` (formerly `admin.py`) lives
here and not in the runtime: a dry-run is a property of the
definition, not of any run.

**If it describes one run, it belongs in `instance/`.** The instance
models carry no behavior beyond their own shape; the store owns the
`.process-state/` layout (`active/`, `completed/`, `abandoned/`,
`log.txt`) and is the only module that writes it.

## Design rules

These are the principles from [`docs/principles/`](principles/) as
they apply to code structure, plus rules this refactor established:

1. **One implementation per semantic.** If two code paths can reach
   the same state, they must share the code that defines what that
   state means. The pre-kernel engine had transition and signal
   paths that each reimplemented dispatch/terminal handling — and
   they had already drifted (signals to terminal states didn't resume
   parents or fire notifications). Duplication of semantics is how
   trust infrastructure quietly stops being trustworthy.

2. **Decide before doing.** All validation of a request happens
   before any side effect. A dispatch state with a broken
   `immediate` target fails during planning, not after the child was
   already spawned. If a request is going to be rejected, it must be
   rejected with the world unchanged.

3. **Fail open, but never silently.** Fail-open is a trust posture
   (see [fail-open](principles/fail-open.md)): enforcement and hooks
   must not brick a project. But "open" refers to the *decision*, not
   the *evidence* — every swallowed exception logs a warning with a
   traceback. The guard writes them to stderr because stdout carries
   the hook-response protocol.

4. **Public surface only.** Frontends import public names. If a
   frontend needs an underscore-prefixed function, that function is
   misfiled or misnamed — fix the surface, don't reach around it
   (`is_override_file` and `find_turnstile_root` both earned their
   public names this way).

5. **Purity is the test strategy.** The kernel is tested with
   in-memory definitions and instances — no `tmp_path`, no engine,
   no subprocesses (`test_kernel.py`). Runtime tests exercise real
   I/O. When a bug is reported, ask first: is this a wrong decision
   (kernel test) or a wrong execution (runtime test)?

6. **Typed at the core, dicts at the edge.** Inside the engine,
   data moves as pydantic models and dataclasses (`TransitionPlan`,
   `_Arrival`, `ValidationResult`). Serialization to `dict` happens
   once, at the facade boundary, where MCP/CLI need JSON-shaped
   results.

## The engine's request lifecycle

```
        frontend (MCP tool / CLI command)
                    │
                    ▼
              Engine facade  ──────  loads definition + instance
                    │
                    ▼
           kernel.plan_transition     pure: legality, metadata,
           kernel.plan_signal         fail-fast wiring checks
                    │
                    ▼
           runtime gates (async)      on_exit → on_enter, blocking
                    │                 failures return early
                    ▼
           history entry appended     the audit record
                    │
                    ▼
           Engine._apply_entry        arrival semantics, once:
             dispatch → spawn child, auto-continue
             wait     → block on signal
             subprocess → spawn child, suspend parent
             terminal → complete, resume parent, notify, summarize
             normal   → save
                    │
                    ▼
           TransitionResult / dict    serialized at the boundary
```

## Known debts (deliberate, not accidental)

- `turnstile-cli/main.py` is a single 1,200-line Click module. It
  should split into command groups (definitions, instances, setup,
  enforcement) mirroring the core layers. Mechanical, low-risk,
  not yet done.
- `turnstile_cli/principles.py` duplicates `docs/principles/*.md` as
  string constants. The docs should ship as package data with one
  source of truth.
- The MCP server holds a module-global `Engine` keyed to one project
  root. Fine for stdio transport (one process, one project), wrong
  for any future multi-project transport; it should become a
  per-connection factory when that matters.
- `Engine._get_definition` still mixes two cache-invalidation
  strategies (per-call hash check + wholesale `reload()`). The
  auto-reload should move behind the loader with a single policy.
- Signal deliveries do not run the target state's `on_enter` gates or
  actions (transitions do). This asymmetry predates the kernel; it is
  now explicit in one place (`receive_signal`) and should become a
  definition-level choice rather than an engine hardcode.
