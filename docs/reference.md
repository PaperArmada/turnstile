# Reference

Single combined reference for process definitions, MCP tools, the CLI, the registry, and enforcement.

## Contents

- [Process definitions](#process-definitions)
- [MCP tools](#mcp-tools)
- [CLI](#cli)
- [Registry](#registry)
- [Inheritance and overrides](#inheritance-and-overrides)
- [Shared process packages](#shared-process-packages)
- [CI integration](#ci-integration)
- [Git hooks](#git-hooks)
- [State persistence](#state-persistence)
- [Enforcement](#enforcement)
- [Starter pack](#starter-pack)

## Process definitions

### States

Every process needs exactly one `initial` state and at least one `terminal` state:

```yaml
states:
  - id: start
    type: initial          # initial, normal, terminal, subprocess, dispatch, wait
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
| `equals("value")` | exact match |
| `not_equals("value")` | not equal |
| `contains("substring")` | substring match |
| `starts_with("prefix")` | prefix match |
| `ends_with("suffix")` | suffix match |
| `matches("regex")` | regex match |
| `greater_than(N)` | numeric comparison |
| `less_than(N)` | numeric comparison |
| `exit_code(N)` | process exit code |

#### Composite assertions

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

### Subprocess delegation

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

## MCP tools

| Tool | Purpose |
|------|---------|
| `process_list` | Show available process definitions |
| `process_info` | Show a definition's parameters, states, and gates |
| `process_start` | Start a new process instance |
| `process_status` | Check active instances and their current state |
| `process_transition` | Move to the next state (runs validation gates) |
| `process_signal` | Deliver a signal to a waiting instance |
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
| `process_reload_definitions` | Reload process YAMLs without restarting the server |

## CLI

```bash
# Validate a process definition
turnstile validate .processes/feature-deploy.yaml

# List available processes
turnstile list

# Show active instances
turnstile status
turnstile status --all

# Show the recorded trajectory of an instance (active or archived):
# every transition with actor attribution (triggered_by, role, session),
# validation gate results, structured metadata, and the override log
turnstile history a3f2dd9c01b7
turnstile history a3f2dd9c01b7 --json

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

# Enforcement
turnstile enforce status
turnstile enforce on
turnstile enforce monitor
turnstile enforce off
```

All commands accept `--project / -p` to specify the project root (defaults to CWD).

When running from the turnstile repo itself, prefix with `uv run --package turnstile-cli`:

```bash
uv run --package turnstile-cli turnstile validate .processes/feature-deploy.yaml
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
    # ${var} references are expanded by the shell from environment variables:
    # name, instance_id, state (on_complete); step, user, reason (on_override).
    # Use double quotes so the shell expands them.
    on_complete: 'echo "Process ${name} completed" >> .process-state/log.txt'
    on_override: 'echo "OVERRIDE: ${step} skipped by ${user}: ${reason}" >> .process-state/log.txt'
```

Without a registry, turnstile auto-discovers all `.yaml` files in `.processes/`.

## Inheritance and overrides

Specialize an existing process definition with an override file: a YAML file with an `extends` key and an `overrides` block in place of a full `states` list. Override files can live in `.processes/overrides/` or directly in `.processes/` (listed in the registry like any other definition). The parent may be a local definition or one pulled from an extended source; override files inside extended-source directories themselves are not supported. The starter pack's `security-review` uses this to specialize `peer-review`.

Naming rules: an override with its own `name` creates a new process alongside the parent. An override without a `name` patches the parent in place, whatever that name finally resolves to. An override whose name collides with an unrelated local definition is rejected at load time.

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

## Shared process packages

Scaffold a distributable Python package of process definitions:

```bash
turnstile init-package acme-processes -P release -P review
```

This generates a package with a `turnstile.processes` entry point that other projects can install and reference via the registry `extends` field.

## CI integration

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

## Git hooks

Install pre-push hooks that enforce process completion before pushing:

```bash
turnstile hooks install pre-push -p feature-deploy:merge
```

This generates a hook that calls `turnstile check-completed` and blocks the push if no matching completed instance is found. Turnstile-managed hooks can be safely replaced or uninstalled; existing non-turnstile hooks are backed up.

## State persistence

Process state is stored in `.process-state/` (add to `.gitignore`):

```
.process-state/
  active/          # Currently running instances
  completed/       # Finished instances (archived by month)
  abandoned/       # Abandoned instances (archived by month)
  log.txt          # Append-only human-readable log
  events.jsonl     # Append-only typed event stream (shadow)
```

Each instance is a JSON file containing the current state, full history, parameters, and any overrides.

### Event stream

Every state mutation appends one (or a documented set of) typed events to
`events.jsonl`: one compact JSON object per line, with `event_type`,
`instance_id`, `process_name`, a per-instance `seq`, `at`, `session_id`,
`actor`, and `payload`. Event types: `started`, `transition`, `dispatch`,
`subprocess_started`, `signal_received`, `skip`, `undo`, `abandon`,
`complete`, `handoff`, `parent_resumed`. Transition-shaped payloads carry
the full history-entry data.

Consumer contract for the stream, which is a **shadow record** while the
instance JSON remains the source of truth:

- Events are appended immediately before the persist that lands the
  mutation, and emission is fail-open. Two consequences: the stream can
  contain an event for a mutation that failed to persist, and a dropped
  append reuses its sequence number. Consumers must treat instance JSON as
  authoritative and dedupe on `(instance_id, seq)`, keeping the last
  occurrence.
- Sequence numbers assume a single writer per instance; concurrent
  sessions mutating one instance can produce duplicate numbers.
- The stream is not fsynced (instance writes are), so a crash can lose
  tail events that the instance JSON reflects.
- An engine older than the stream that loads and re-saves an instance
  resets its `seq` counter, after which new events reuse low numbers.
- The stream never expires: parameters, signal data, and metadata written
  to it persist even after instance files are deleted.

## Enforcement

Turnstile can enforce process compliance by integrating with Claude Code's PreToolUse hooks. When enabled, the Edit and Write tools are gated on having an active process instance whose current state permits edits.

The hook matches the Edit and Write tools only; it does not gate the Bash tool or parse the contents of shell commands. Command-level file mutations (for example `sed -i`, `tee`, or a shell redirect) are therefore outside its scope: Turnstile does not inspect shell command text to decide whether a command mutates a file.

Three modes:

| Mode | Behavior |
|------|----------|
| `off` | No enforcement (default) |
| `monitor` | Warns when no active process (or the state forbids edits), but allows the action |
| `enforce` | Blocks Edit/Write when no active process permits the edit |

```bash
# Enable enforcement (updates registry.yaml and installs Claude Code hook)
turnstile enforce on

# Enable monitor mode (warnings only)
turnstile enforce monitor

# Disable enforcement
turnstile enforce off

# Check current status
turnstile enforce status
```

Enforcement is configured in `registry.yaml` under `settings.enforcement` and works via a PreToolUse hook in `.claude/settings.json` that calls `turnstile guard`. When several process instances are active, the most restrictive one wins: an edit is blocked if any active instance forbids it (a second, more permissive instance cannot lift another's restriction). The guard fails open on unexpected internal errors so a bug never hard-blocks the agent, with one deliberate exception: when enforcement is on but its state cannot be determined — a corrupt state file, an unreadable `registry.yaml`, a definition that will not load, or an unresolved definition for a live instance — the guard fails closed rather than silently allowing. It blocks in `enforce` mode and warns in `monitor` mode; if the failure prevents reading the mode itself (an unreadable `registry.yaml`), it blocks unconditionally, because it cannot prove enforcement is off.

## Starter pack

Turnstile ships with a curated set of processes in [`.processes/`](../.processes/).

General-purpose (work in any domain, not just software):

| Process | Purpose | States |
|---------|---------|--------|
| `peer-review` | Structured peer review with role handoff | prepare → awaiting_review → revise → accepted |
| `decision-record` | Structured decision-making with auditable record | frame → gather → evaluate → decide → communicate |
| `scientific-method` | Structured investigation | observe → hypothesize → test → conclude |
| `security-review` | Security-focused review; extends `peer-review` via inheritance | prepare → awaiting_review → revise → accepted |

Developer-oriented:

| Process | Purpose | States |
|---------|---------|--------|
| `feature-development` | Standard feature workflow | understand → implement → test → review → done |
| `bug-fix` | Structured bug fix: reproduce, diagnose, fix, verify | reproduce → diagnose → fix → verify → done |
| `code-review` | Review a changeset or pull request | survey → review → request_changes / approve |
| `release` | Prepare and ship a versioned release | prepare → validate → tag → done |
| `spike` | Time-boxed investigation with a written outcome | investigate → write_up → done / abandoned |

Meta (processes for managing processes):

| Process | Purpose | States |
|---------|---------|--------|
| `create-process` | Design and validate a new process definition | understand → draft → review → done |
| `readme-update` | Keep the README aligned with the codebase | audit → update → verify → done |

Each process uses info-severity gates to surface relevant context (recent commits, diff stats, working tree status) and back-transitions for forgiveness. They are generic by design, using git as the common denominator rather than project-specific tooling.

To use the starter pack in your project, copy the desired YAML files from `.processes/` or reference them via the registry `extends` field. Additional definitions that showcase specific engine features (branching, dispatch, wait states) live in [`examples/`](../examples/).
