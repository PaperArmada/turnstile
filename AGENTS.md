# Agent Instructions

## Issue Tracking

This project uses **bd** (beads) for issue tracking. Run `bd prime` for workflow context.

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
bd sync               # Sync with git
```

## Process Enforcement

This project uses **turnstile** for workflow enforcement (it is also the project being built).

```bash
turnstile list        # Available process definitions
turnstile status      # Active process instances
turnstile validate <path>  # Validate a process YAML file
```

The MCP server is configured in `.mcp.json` for conversational use via Claude Code.

## Project Structure

```
packages/
  turnstile-core/     # Core engine: models, loader, validator, persistence, engine
  turnstile-mcp/      # MCP server wrapping the core
  turnstile-cli/      # CLI tool
```

**Run tests:** `uv run --package turnstile-core pytest packages/turnstile-core/tests/ -v`

## Principles

See `docs/principles/` for design principles that guide this project:
- Progressive Disclosure
- Render, Don't Record
- Single Process, Single Document

## Landing the Plane (Session Completion)

When ending a work session, complete ALL steps:

1. File issues for remaining work
2. Run quality gates (if code changed)
3. Update issue status
4. Push to remote:
   ```bash
   git pull --rebase
   bd sync
   git push
   ```
5. Verify all changes committed and pushed
6. Provide context for next session
