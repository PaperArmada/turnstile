"""Design principles for turnstile process definitions.

Shipped as part of `turnstile init` to give consumers the rationale
behind the engine's design. Written to .processes/principles/ in the
consumer project.
"""

PRINCIPLES: dict[str, str] = {
    "progressive-disclosure": """\
# Progressive Disclosure

> Adapted from Nielsen Norman Group's definition: "Defer advanced or
> rarely used features to a secondary screen, making applications easier
> to learn and less error-prone."

**Show the minimum needed at each level of depth, with clear links to
the next level. Never duplicate details across levels.**

## Rules

1. **Each document serves one level of depth.** Parent docs provide the
   map; child docs own the details. A reader at any level should see
   just enough to orient themselves and know where to go next.

2. **A fact lives in exactly one document.** Other documents reference it
   with a link. If you need to change a fact, you change it in one
   place. If you find the same explanation in two documents, one of them
   is wrong (or will be, eventually).

3. **Links set expectations.** A cross-reference should tell the reader
   what they'll find before they follow it. "See [Deployment](deployment.md)"
   is insufficient. "See [Deployment: rollback procedure](deployment.md#rollback)"
   tells them exactly what's on the other side.

## How to apply it

When writing or editing a document, ask:

- **Am I explaining something that's already explained elsewhere?**
  Link to it instead.
- **Would a reader at this level need this detail to do their immediate
  task?** If not, it belongs one level deeper.
- **If I deleted this paragraph, would the information still exist
  somewhere in the docs?** If yes, delete it and link. If no, keep it
  (this is the canonical location).
""",
    "render-dont-record": """\
# Render, Don't Record

**If a fact can change without a commit, the document should contain the
query that retrieves it, not a snapshot of the answer.**

## Rules

1. **Mutable state belongs in systems, not files.** When a document needs
   to present dynamic information, it should provide the command that
   constructs the current truth. A hardcoded table is correct once and
   wrong forever after.

2. **Templates use placeholders, not filled-in examples.** Version
   numbers, dates, issue IDs, and other identifiers in process docs
   should appear as `vX.Y.Z`, `YYYY-MM-DD`, `#NNN`. A filled-in example
   invites copy-paste without substitution; a placeholder forces the
   reader to supply the real value.

3. **Records are not state.** A changelog entry documenting what shipped
   is an immutable historical fact. The test: "Will this be wrong
   tomorrow without someone updating it?" If yes, it's state (render
   it). If no, it's a record (write it).

## The query pattern

Instead of hardcoding values, provide the command that retrieves current
truth:

```bash
# Instead of "there are 12 open issues", use:
git log --oneline --no-merges main..HEAD | wc -l

# Instead of a hardcoded test coverage number, use:
pytest --cov --cov-report=term-missing | tail -1
```

The document stays correct indefinitely because it never contains the
answer, only the question.
""",
    "single-process-single-document": """\
# Single Process, Single Document

If a workflow runs as one pass, document it in one file. Split into
separate documents only when sub-steps are independently triggered on
different cadences or by different roles.

## Rules

1. **One pass, one document.** If an operator sits down and runs steps A
   through F in sequence, those steps belong in a single document.
   Splitting them across files forces the reader to navigate between
   documents mid-process, increasing the chance they skip a step or lose
   context.

2. **Split on trigger, not on source.** A review pass that covers bug
   reports, feature requests, and support tickets is one process with
   three inputs, not three processes. The trigger is the same ("run the
   review"), the operator is the same, and the output is the same.
   Separate documents are warranted when different events trigger
   different subsets of work on genuinely different schedules.

3. **Cross-cutting concerns need a single home.** When a concept spans
   the entire process (e.g., a shared timestamp anchor, a labeling
   convention), it must live in the document that owns the process. If
   the process is split across files, cross-cutting concerns have no
   natural home and end up duplicated or missing.
""",
    "fresh-install": """\
# Fresh Install

> Every artifact, example, and default must work for someone installing
> the software for the first time on a machine you've never seen.

**Design as if the next user has zero context about your development
environment.**

## Rules

1. **No hardcoded paths.** Absolute paths to specific machines, home
   directories, or clone locations must never appear in committed code,
   configuration templates, or documentation examples. Use discovery
   (env vars, relative paths, package resolution) or clearly marked
   placeholders.

2. **Configuration must be portable.** If a config file works on your
   machine but breaks on someone else's, it's a bug. Machine-specific
   values belong in gitignored files, environment variables, or generated
   output, never in tracked sources.

3. **Examples must be copy-pasteable.** Documentation examples should
   work after a single substitution (e.g., replacing `/path/to/X`), not
   require the reader to understand your project layout or conventions.

4. **Defaults must be self-contained.** The software should do something
   useful with zero configuration beyond what's generated during
   installation. Every required input that can be discovered at runtime
   should be.

5. **Test the setup path, not just the happy path.** If the first-run
   experience is broken, nothing else matters. The distance from
   `git clone` to working software is the most important metric.
""",
    "fail-open": """\
# Fail Open

> When enforcement infrastructure encounters an error, it must allow the
> action to proceed rather than blocking it.

**The cost of a false block (agent stuck, developer waiting) is always
higher than the cost of a missed check.**

## Rules

1. **Errors allow, never block.** If the guard can't read the registry,
   find the state directory, parse JSON, or complete any check, the
   action proceeds. A broken guardrail that blocks work is worse than no
   guardrail at all.

2. **Degrade gracefully across layers.** Each enforcement layer (entry
   guard, state machine gates, CI checks) should function independently.
   A failure in one layer must not cascade to block the others.

3. **Log failures visibly.** Failing open silently hides problems. When
   enforcement degrades, surface that it degraded so the issue can be
   fixed. The agent or user should know the guard was skipped, even if
   the action was allowed.

4. **Default to off, not on.** New enforcement features start disabled.
   The user opts in after verifying the system works. An enforcement mode
   that ships enabled and then breaks on first use destroys trust
   permanently.
""",
    "50-first-dates": """\
# 50 First Dates

> Every agent session starts from zero. The only knowledge an agent has
> of your project or tools is what you make available in context.

**Agents have no persistent memory of your project. If it's not in
context, it doesn't exist.**

## Rules

1. **Context is the only interface.** An agent cannot remember what
   happened last session, what conventions you prefer, or what your
   codebase looks like. Everything it needs must be surfaced through
   CLAUDE.md, process definitions, validation gates, or tool responses.

2. **Design for the blank slate.** Every artifact (process definitions,
   gate commands, error messages, tool descriptions) should be
   self-explanatory to an agent encountering the project for the first
   time. If understanding requires prior sessions, the design has failed.

3. **Encode knowledge in durable artifacts, not conversation.** Decisions,
   conventions, and patterns belong in files (CLAUDE.md, process YAML,
   config), not in chat history. Chat history is ephemeral; files survive
   across sessions.

4. **Validation gates are context delivery.** Gates don't just check
   conditions. They surface information the agent needs at the moment it
   needs it. A gate that runs `git log --oneline -5` isn't just verifying
   commits exist; it's teaching the agent what happened recently.
""",
    "rough-cuts": """\
# Rough Cuts

> Move fast and remove material aggressively when far from critical
> surfaces. Dial in precision only where tolerances are tight.

**Not all work requires the same level of care. Match your precision to
the proximity of the final surface.**

## Rules

1. **Hog out material early.** When exploring, prototyping, or building
   scaffolding, speed matters more than polish. Get the shape right
   before worrying about the finish. A rough-cut spike that answers the
   question in an hour beats a polished implementation that takes a day.

2. **Identify critical surfaces.** Know which parts of the system have
   tight tolerances: public APIs, data persistence formats, security
   boundaries, user-facing messages. These deserve careful attention.
   Everything else can be rougher.

3. **Progressive refinement, not uniform precision.** First pass: does it
   work at all? Second pass: does it handle edge cases? Third pass: is
   it clean? Don't jump to the third pass on code that might not survive
   the first.

4. **Ship the rough cut, then iterate.** A working rough version that can
   be tested and improved is more valuable than a theoretical perfect
   version. Real feedback from real use (friction testing, dogfooding)
   reveals what actually needs precision.
""",
    "portable-by-default": """\
# Portable by Default

> Process definitions should work in any project that adopts them, not
> just the project where they were written.

**Validation commands, paths, and tool invocations in shared definitions
must not assume the authoring project's environment.**

## Rules

1. **Parameterize project-specific commands.** If a validation gate runs
   the test suite, the test command should be a parameter with a generic
   default (e.g., `pytest`), not a hardcoded invocation tied to your
   monorepo layout. The consumer overrides the parameter at start time.

2. **Prefer universal tools in gates.** `git status`, `git diff`,
   `test -f` work everywhere. `uv run --package my-thing pytest
   packages/my-thing/tests/` works in exactly one project. Gates in
   shared definitions should rely on the former.

3. **Author-specific definitions are fine.** Not everything needs to be
   portable. A project's own `feature-development.yaml` can reference
   project-specific tooling. But anything tagged `starter-pack` or
   distributed via `extends` must work without modification in consumer
   projects.

4. **Failures train behavior.** When a validation command fails because
   it references a nonexistent path, the agent learns to ignore
   validation results entirely. A gate that always fails is worse than no
   gate at all, because it erodes trust in the system.

## Relationship to Other Principles

- **Fresh Install** covers paths and configuration. Portable by Default
  covers the *content* of process definitions.
- **Fail Open** means broken gates don't block the agent. But Portable by
  Default asks: why are they broken in the first place?
- **Render Don't Record** says gates should query live state. Portable by
  Default adds: the queries must be valid in the consumer's environment,
  not just yours.
""",
}
