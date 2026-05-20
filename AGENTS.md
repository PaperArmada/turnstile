# Agent Instructions

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

1. Run quality gates (if code changed)
2. Push to remote:
   ```bash
   git pull --rebase
   git push
   ```
3. Verify all changes committed and pushed
4. Provide context for next session
