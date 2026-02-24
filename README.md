# Turnstile

A local, single-user process enforcement engine for AI agent workflows.

Turnstile is a deterministic state machine that sits between developer intent and agent execution. It enforces multi-step procedures by requiring agents to advance through defined states, running validation gates at each transition, and rejecting illegal moves. Process definitions are YAML files in your repository. State is persisted to disk and survives session restarts.

Exposed as an MCP server for use with Claude Code (or any MCP-compatible client).

## How It Works

1. You define a process as a YAML file describing states, transitions, and validation gates
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

## Using Turnstile in Your Project

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

Replace `/path/to/turnstile` with the absolute path to your turnstile clone.

Restart Claude Code. The process tools will be available automatically.

### 3. Use the tools

Once registered, the following tools are available in conversation:

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

A typical session looks like:

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

### 4. State persistence

Process state is stored in `.process-state/` (add to `.gitignore`):

```
.process-state/
  active/          # Currently running instances
  completed/       # Finished instances (archived by month)
  abandoned/       # Abandoned instances (archived by month)
  log.txt          # Append-only event log
```

Each instance is a JSON file containing the current state, full history, parameters, and any overrides.

### 5. Optional registry

For projects with multiple process definitions, create a `.processes/registry.yaml`:

```yaml
version: "1.0"
local:
  - feature-deploy
  - hotfix
settings:
  state_dir: .process-state
  require_override_reason: true
  log_retention_days: 90
```

Without a registry, turnstile auto-discovers all `.yaml` files in `.processes/`.

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

### Validation gates

Gates run a shell command and check its output:

```yaml
validate:
  - command: "pytest -q 2>&1; echo $?"
    expect: ends_with("0")
    message: "Tests must pass"
    severity: error        # error (blocks), warning (logs), info (always allows)
    timeout: 60            # seconds, default 60
```

#### Expect expressions

| Expression | Checks |
|---|---|
| `empty` | stdout is empty |
| `not_empty` | stdout is not empty |
| `equals("value")` | stdout equals value exactly |
| `not_equals("value")` | stdout does not equal value |
| `contains("substring")` | stdout contains substring |
| `starts_with("prefix")` | stdout starts with prefix |
| `ends_with("suffix")` | stdout ends with suffix |
| `matches("regex")` | stdout matches regex pattern |
| `greater_than(N)` | stdout as integer > N |
| `less_than(N)` | stdout as integer < N |
| `exit_code(N)` | process exit code equals N |

#### Composite assertions

```yaml
validate:
  - any:                   # OR: at least one must pass
      - command: "test -f coverage.xml"
        expect: exit_code(0)
        message: "coverage.xml exists"
      - command: "test -f coverage.json"
        expect: exit_code(0)
        message: "coverage.json exists"
    message: "Need at least one coverage report"

  - all:                   # AND: all must pass
      - command: "echo check1"
        expect: not_empty
        message: "first check"
      - command: "echo check2"
        expect: not_empty
        message: "second check"
```

### Parameters

Process definitions can declare parameters that are substituted into commands:

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

### Evidence-based validation

Check that a file exists and is recent enough:

```yaml
validate:
  - command: "cat test-results.json | jq '.passed'"
    expect: equals("true")
    message: "Tests must have passed"
    evidence: test-results.json
    max_age: 30m           # 30m, 1h, 90s
```

## CLI

```bash
# Validate a process definition
uv run --package turnstile-cli turnstile validate .processes/feature-deploy.yaml

# List available processes
uv run --package turnstile-cli turnstile list

# Show active instances
uv run --package turnstile-cli turnstile status
uv run --package turnstile-cli turnstile status --all
```

## Project Structure

```
packages/
  turnstile-core/     # Models, loader, validator, persistence, engine
  turnstile-mcp/      # MCP server (FastMCP, stdio transport)
  turnstile-cli/      # CLI (Click)
```

## Running Tests

```bash
uv run --package turnstile-core pytest packages/turnstile-core/tests/ -v
```

## License

MIT
