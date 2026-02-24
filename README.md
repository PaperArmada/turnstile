# Turnstile

A local, single-user process enforcement engine for AI agent workflows.

Turnstile is a deterministic state machine that sits between developer intent and agent execution. It enforces multi-step procedures by requiring agents to advance through defined states, running validation gates at each transition, and rejecting illegal moves. Process definitions are YAML files in your repository. State is persisted to disk and survives session restarts.

Exposed as an MCP server for use with Claude Code (or any MCP-compatible client), with a CLI for validation, CI integration, and administration.

## How It Works

1. Define a process as a YAML file describing states, transitions, and validation gates
2. An agent (or human) starts an instance of that process
3. The engine tracks the current state and only allows legal transitions
4. Validation gates run shell commands and check their output before allowing transitions
5. Everything is logged: transitions, overrides, abandoned processes

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/PaperArmada/turnstile.git
cd turnstile
uv sync --all-packages
```

## Quick Start

### 1. Create a process definition

Create a `.processes/` directory in your project root and add YAML files:

```yaml
# .processes/feature-deploy.yaml
name: feature-deploy
description: "Standard feature branch workflow"
version: "1.0.0"

parameters:
  - name: branch_name
    description: "Feature branch name"
    required: true

states:
  - id: start
    type: initial
    transitions: [implement]

  - id: implement
    description: "Write the feature code"
    transitions: [test]

  - id: test
    description: "Run the test suite"
    transitions: [review, implement]
    on_exit:
      validate:
        - command: "pytest -q 2>&1; echo $?"
          expect: ends_with("0")
          message: "Tests must pass before review"

  - id: review
    description: "Code review"
    transitions: [merge, implement]
    on_enter:
      validate:
        - command: "git log main..HEAD --oneline | wc -l"
          expect: greater_than(0)
          message: "Must have commits ahead of main"

  - id: merge
    transitions: [done]

  - id: done
    type: terminal
```

### 2. Register the MCP server

Add a `.mcp.json` to your project root:

```json
{
  "mcpServers": {
    "turnstile": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "--directory", "/path/to/turnstile",
        "run", "--package", "turnstile-mcp",
        "python", "-m", "turnstile_mcp.server"
      ]
    }
  }
}
```

Replace `/path/to/turnstile` with the absolute path to your turnstile clone. Restart Claude Code and the process tools will be available automatically.

### 3. Use the tools

A typical session:

```
You: Start the feature-deploy process for branch auth-refactor
Agent: [calls process_start] Started instance a3f2dd at "start"

You: Move to implement
Agent: [calls process_transition] Transitioned to "implement"

You: I've written the code and tests. Move to test.
Agent: [calls process_transition] Transitioned to "test"

You: Move to review
Agent: [calls process_transition, which runs pytest]
       Validation passed (116 tests, 0 failures). Transitioned to "review"

You: Skip straight to done, the reviewer approved in Slack
Agent: [calls process_skip with reason] Skipped to "done". Override logged.
```

## MCP Tools

| Tool | Purpose |
|------|---------|
| `process_list` | Show available process definitions |
| `process_start` | Start a new process instance |
| `process_status` | Check active instances and their current state |
| `process_transition` | Move to the next state (runs validation gates) |
| `process_skip` | Force-skip a state with a logged reason |
| `process_abandon` | Abandon a process instance |
| `process_undo` | Revert the last transition |
| `process_handoff` | Transfer ownership (metadata) |
| `process_history` | View full transition history |
| `process_validate_definition` | Validate a YAML file against the schema |
| `process_graph` | Generate a Mermaid state diagram |
| `process_dry_run` | Simulate a process without executing commands |
| `process_diff` | Compare two process definition files |
| `process_migrate` | Check if an in-flight instance needs migration |
| `process_analytics` | Compute stats from archived instances |
| `process_check_completed` | Verify a process was completed (for CI/hooks) |

## CLI

```bash
# Validate a process definition
turnstile validate .processes/feature-deploy.yaml

# List available processes
turnstile list

# Show active instances
turnstile status
turnstile status --all

# Generate a Mermaid state diagram
turnstile graph feature-deploy

# Simulate a process execution
turnstile dry-run feature-deploy
turnstile dry-run feature-deploy --path start --path implement --path test

# Export JSON Schema for editor autocomplete
turnstile schema
turnstile schema --output ./schemas/

# Check process completion (for CI pipelines)
turnstile check-completed feature-deploy --state merge
turnstile check-completed feature-deploy -P branch_name=main -s review --json

# Process analytics
turnstile analytics
turnstile analytics --json

# Git hook management
turnstile hooks install pre-push -p feature-deploy:merge
turnstile hooks show pre-push -p feature-deploy:merge
turnstile hooks uninstall pre-push

# Scaffold a shared process package
turnstile init-package my-processes -P release -P review
```

All commands accept `--project / -p` to specify the project root (defaults to CWD).

When running from the turnstile repo itself, prefix with `uv run --package turnstile-cli`:

```bash
uv run --package turnstile-cli turnstile validate .processes/feature-deploy.yaml
```

## Process Definition Reference

### States

Every process needs exactly one `initial` state and at least one `terminal` state:

```yaml
states:
  - id: start
    type: initial          # initial, normal, terminal, subprocess
    transitions: [next]    # legal target states

  - id: next
    description: "Do something"
    transitions: [done]
    on_enter:
      validate: [...]      # gates that must pass to enter this state
      actions: [...]       # side-effect commands (best-effort)
    on_exit:
      validate: [...]      # gates that must pass to leave this state

  - id: done
    type: terminal         # no transitions allowed
```

### Validation Gates

Gates run a shell command and check its output:

```yaml
validate:
  - command: "pytest -q 2>&1; echo $?"
    expect: ends_with("0")
    message: "Tests must pass"
    severity: error        # error (blocks), warning (logs), info (always allows)
    timeout: 60            # seconds, default 60
```

#### Expect Expressions

| Expression | Checks |
|---|---|
| `empty` | stdout is empty |
| `not_empty` | stdout is not empty |
| `equals("value")` | exact match |
| `not_equals("value")` | not equal |
| `contains("substring")` | substring match |
| `starts_with("prefix")` | prefix match |
| `ends_with("suffix")` | suffix match |
| `matches("regex")` | regex match |
| `greater_than(N)` | numeric comparison |
| `less_than(N)` | numeric comparison |
| `exit_code(N)` | process exit code |

#### Composite Assertions

```yaml
validate:
  - any:                   # OR: at least one must pass
      - command: "test -f coverage.xml"
        expect: exit_code(0)
      - command: "test -f coverage.json"
        expect: exit_code(0)
    message: "Need at least one coverage report"

  - all:                   # AND: all must pass
      - command: "echo check1"
        expect: not_empty
      - command: "echo check2"
        expect: not_empty
```

### Parameters

Parameters are substituted into commands using `${var_name}` syntax:

```yaml
parameters:
  - name: branch_name
    required: true
  - name: ticket_id
    required: false
    default: "none"

states:
  - id: done
    type: terminal
    on_enter:
      actions:
        - command: "echo 'Deployed ${branch_name} for ${ticket_id}'"
```

### Evidence-Based Validation

Check that a file exists and is recent enough:

```yaml
validate:
  - command: "cat test-results.json | jq '.passed'"
    expect: equals("true")
    message: "Tests must have passed"
    evidence: test-results.json
    max_age: 30m           # 30m, 1h, 90s
```

### Subprocess Delegation

A state can delegate to another process definition. The parent is suspended until the child completes or is abandoned:

```yaml
states:
  - id: test
    type: subprocess
    process: integration-tests
    parameter_map:
      target_env: "${deploy_target}"
    subprocess_routing:
      on_complete: [deploy]     # where parent goes when child finishes
      on_fail: [rollback]       # where parent goes if child is abandoned
```

## Registry

For projects with multiple definitions or external sources, create `.processes/registry.yaml`:

```yaml
version: "1.0"

extends:
  - source: "./shared-processes"           # local directory
  - source: "git+https://github.com/org/processes.git@v2"  # git repo
  - source: "acme-processes"               # Python package (entry point)
    processes: [release, deploy]            # optional filter

local:
  - feature-deploy
  - hotfix

settings:
  state_dir: .process-state
  require_override_reason: true
  log_retention_days: 90
  notifications:
    on_complete: "echo 'Process {name} completed' >> .process-state/log.txt"
    on_override: "echo 'OVERRIDE: {step} skipped by {user}: {reason}' >> .process-state/log.txt"
```

Without a registry, turnstile auto-discovers all `.yaml` files in `.processes/`.

### Inheritance and Overrides

Override an inherited process definition by placing a YAML file in `.processes/overrides/`:

```yaml
# .processes/overrides/feature-deploy.yaml
extends: feature-deploy

overrides:
  add_states:
    - id: security_scan
      description: "Run security checks"
      transitions: [review]
      on_enter:
        validate:
          - command: "semgrep --config auto ."
            expect: exit_code(0)
            message: "Security scan must pass"

  patch_states:
    - id: test
      transitions: [security_scan, implement]   # replaces original transitions

  parameters:
    append:
      - name: scan_level
        default: "standard"
```

### Shared Process Packages

Scaffold a distributable Python package of process definitions:

```bash
turnstile init-package acme-processes -P release -P review
```

This generates a package with a `turnstile.processes` entry point that other projects can install and reference via the registry `extends` field.

## CI Integration

Use `check-completed` in CI pipelines to verify process compliance:

```yaml
# GitHub Actions
- name: Verify process completion
  run: |
    turnstile check-completed feature-deploy \
      --parameter branch_name=${{ github.head_ref }} \
      --state review
```

Exit code 0 means a matching completed instance was found, 1 means it was not.

### Git Hooks

Install pre-push hooks that enforce process completion before pushing:

```bash
turnstile hooks install pre-push -p feature-deploy:merge
```

This generates a hook that calls `turnstile check-completed` and blocks the push if no matching completed instance is found. Turnstile-managed hooks can be safely replaced or uninstalled; existing non-turnstile hooks are backed up.

## State Persistence

Process state is stored in `.process-state/` (add to `.gitignore`):

```
.process-state/
  active/          # Currently running instances
  completed/       # Finished instances (archived by month)
  abandoned/       # Abandoned instances (archived by month)
  log.txt          # Append-only event log
```

Each instance is a JSON file containing the current state, full history, parameters, and any overrides.

## Dogfooding

Turnstile uses its own processes in `.processes/`:

- **readme-update**: Guides README updates with audit, update, and verify states. Validation gates query the codebase (module list, MCP tool names, CLI commands) to check completeness rather than relying on self-reported checklists.
- **feature-development**: Standard development workflow with test gates.

These processes exercise the design principles documented in `docs/principles/`:

- **Progressive disclosure**: Each state reveals only what's needed for the current step.
- **Render don't record**: Gates compute current truth from the codebase instead of checking static assertions.
- **Single process, single document**: Each workflow is one process definition, not split across files.

## Project Structure

```
packages/
  turnstile-core/     # Models, loader, validator, persistence, engine,
                      # analytics, hooks, notifications, inheritance,
                      # registry, schema, template
  turnstile-mcp/      # MCP server (FastMCP, stdio transport)
  turnstile-cli/      # CLI (Click)
docs/
  principles/         # Design principles
  process-engine-spec.md  # Full specification
```

## Running Tests

```bash
uv run --package turnstile-core pytest packages/turnstile-core/tests/ -v
```

## License

MIT
