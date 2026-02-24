# Turnstile Project Instructions

## Process Enforcement

This project uses its own process engine. Enforcement is set to **monitor** mode: you will receive warnings when making file changes without an active process.

**Before starting work, start the appropriate process:**

| Task | Process | Example |
|------|---------|---------|
| New feature or improvement | `feature-development` | `process_start("feature-development", {"feature_name": "enforcement"})` |
| Creating a new process definition | `create-process` | `process_start("create-process", {"name": "my-process", "purpose": "..."})` |
| Updating the README | `readme-update` | `process_start("readme-update", {"reason": "new CLI commands"})` |

Use `process_status` to check active instances. Use `process_transition` to advance through states. Follow the validation gates; they surface useful context.

## Development Commands

```bash
# Run tests
uv run --package turnstile-core pytest packages/turnstile-core/tests/ -v

# Validate a process definition
uv run --package turnstile-cli turnstile validate .processes/<name>.yaml

# List processes
uv run --package turnstile-cli turnstile list

# Check enforcement status
uv run --package turnstile-cli turnstile enforce status
```

## Git Workflow

Direct pushes to main are allowed for this project.
