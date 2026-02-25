# Portable by Default

> Process definitions should work in any project that adopts them, not just the project where they were written.

**Validation commands, paths, and tool invocations in shared definitions must not assume the authoring project's environment.**

## Rules

1. **Parameterize project-specific commands.** If a validation gate runs the test suite, the test command should be a parameter with a generic default (e.g., `pytest`), not a hardcoded invocation tied to your monorepo layout. The consumer overrides the parameter at start time.

2. **Prefer universal tools in gates.** `git status`, `git diff`, `test -f` work everywhere. `uv run --package my-thing pytest packages/my-thing/tests/` works in exactly one project. Gates in shared definitions should rely on the former.

3. **Author-specific definitions are fine.** Not everything needs to be portable. A project's own `feature-development.yaml` can reference project-specific tooling. But anything tagged `starter-pack` or distributed via `extends` must work without modification in consumer projects.

4. **Failures train behavior.** When a validation command fails because it references a nonexistent path, the agent learns to ignore validation results entirely. A gate that always fails is worse than no gate at all, because it erodes trust in the system.

## Anti-patterns

- Test gates that hardcode a specific package manager invocation (`uv run --package X pytest packages/X/tests/`)
- Validation commands that reference paths relative to the authoring repo's directory structure
- Shared definitions that require the consumer to edit YAML to fix broken commands
- Distributing definitions via `extends` that only work in the source project

## Relationship to Other Principles

- **Fresh Install** covers paths and configuration. Portable by Default covers the *content* of process definitions.
- **Fail Open** means broken gates don't block the agent. But Portable by Default asks: why are they broken in the first place?
- **Render Don't Record** says gates should query live state. Portable by Default adds: the queries must be valid in the consumer's environment, not just yours.
