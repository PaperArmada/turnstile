# Process Engine MCP Server: Design Specification

## Scope

This is a **local, single-user, single-repo** process enforcement engine. It
exists to solve one problem: AI agents working in a codebase cannot be trusted
to follow multi-step procedures reliably. Documentation is advisory. This system
is structural.

The engine enforces **what must happen and in what order** for a given workflow.
It does not perform the work. It does not coordinate between team members. It does
not manage deployments, infrastructure, or access control. Those are solved
problems with existing tools. The engine's job is to guarantee that a sequence of
steps is followed correctly by a single developer (or their AI agent) working in
a single repository on a single machine.

Concretely: it is a deterministic state machine that sits between the developer's
intent and the agent's execution. Process definitions declare states, legal
transitions, and validation gates. The engine persists state to disk, survives
session restarts, rejects illegal transitions, and logs everything. The developer
interacts with it conversationally through Claude Code via MCP tool calls. The
admin authors and maintains process definitions as YAML files in version control.

**What this is:**
- A local enforcement layer for development workflows
- A state machine runtime exposed as an MCP server
- A YAML-based process definition system with schema validation
- An audit trail for how work actually progressed

**What this is not:**
- A team coordination or deployment orchestration system
- A distributed lock manager or queue
- A CI/CD pipeline (though CI can check process state)
- A project management tool (though GPS may integrate with it)

If it requires shared state, networking, or awareness of what other people are
doing, it is out of scope. The engine handles the local workflow leading up to
the checkpoints that team-wide systems already enforce.

---

## Design Principles

1. **Processes are code.** Definitions live in version control, get reviewed in PRs,
   and follow the same lifecycle as the codebase they govern.

2. **Enforce structure, not implementation.** The engine knows that tests must pass
   before merging. It does not know (or care) how the tests get written.

3. **Fail closed, escape explicitly.** Illegal transitions are rejected by default.
   Overrides are possible but require explicit intent and get logged.

4. **Local-first, single-user, single-repo.** No shared state, no networking, no
   distributed coordination. Team-wide concerns belong to the systems built for them.

5. **Project-local by default, shareable by design.** Every project owns its process
   definitions. Common patterns can be extracted into shared registries and extended.

6. **The admin experience is file editing.** No web dashboards, no GUIs, no new
   platforms to learn. YAML files, JSON Schema validation, CLI tooling, and your
   existing editor.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   Developer (CLI)                    │
│          Conversational interface (Claude Code)      │
└──────────────────────┬──────────────────────────────┘
                       │ MCP tool calls
                       ▼
┌─────────────────────────────────────────────────────┐
│              Process Engine MCP Server               │
│                                                      │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐  │
│  │  Definition  │  │   Runtime    │  │   Admin    │  │
│  │   Loader     │  │   Engine     │  │   Tools    │  │
│  │             │  │             │  │            │  │
│  │ - Schema     │  │ - State mgmt │  │ - Validate │  │
│  │ - Inheritance│  │ - Transitions│  │ - Dry run  │  │
│  │ - Resolution │  │ - Validation │  │ - Inspect  │  │
│  │ - Caching    │  │ - Persistence│  │ - Migrate  │  │
│  └─────────────┘  └──────────────┘  └────────────┘  │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │              State Persistence Layer             │ │
│  │         .process-state/ (gitignored)             │ │
│  └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
┌──────────────┐ ┌──────────┐ ┌────────────┐
│  .processes/ │ │ Shared   │ │  Shell /   │
│  (project    │ │ Registry │ │  Commands  │
│   local)     │ │ (npm/git)│ │ (validation│
└──────────────┘ └──────────┘ │  gates)    │
                              └────────────┘
```

---

## Process Definitions

### File Structure

```
my-project/
├── .processes/
│   ├── registry.yaml          # declares which processes this project uses
│   ├── feature-deploy.yaml    # project-specific process
│   ├── hotfix.yaml            # another process
│   └── overrides/
│       └── shared-release.yaml  # local overrides for an inherited process
├── .process-state/            # gitignored, runtime state lives here
│   ├── active/
│   │   ├── feature-deploy-abc123.json
│   │   └── hotfix-def456.json
│   └── completed/
│       └── feature-deploy-xyz789.json
├── CLAUDE.md
└── ...
```

### Registry File

The registry declares what processes are available in this project, where they
come from, and any local configuration.

```yaml
# .processes/registry.yaml
version: "1.0"

# Shared process packs (installed via npm, git submodule, or copied)
extends:
  - source: "@deep-currents/standard-processes"
    version: "^2.0.0"
    processes:
      - release          # import specific processes
      - code-review

  - source: "git+https://github.com/your-org/process-library.git#v1.0"
    processes:
      - database-migration

# Project-local processes (defined in .processes/*.yaml)
local:
  - feature-deploy
  - hotfix

# Global settings for this project
settings:
  state_dir: ".process-state"       # where runtime state lives
  log_retention_days: 90            # how long to keep completed process logs
  require_override_reason: true     # force a reason string on skip/override
  notifications:                    # optional hooks
    on_complete: "echo 'Process {name} completed' >> .process-state/log.txt"
    on_override: "echo 'OVERRIDE: {step} skipped by {user}: {reason}' >> .process-state/log.txt"
```

### Process Definition Schema

```yaml
# .processes/feature-deploy.yaml
name: feature-deploy
description: "Standard feature branch development and deployment workflow"
version: "1.2.0"

# Optional: inherit from a shared process and customize
# extends: "@deep-currents/standard-processes/release"

metadata:
  author: "matt"
  tags: ["deployment", "feature"]
  estimated_duration: "30-60 min"

# Variables that get set when a process instance starts
# These are available in validation commands and templates as ${var_name}
parameters:
  - name: branch_name
    description: "Name of the feature branch"
    required: true
  - name: ticket_id
    description: "Issue tracker ticket ID"
    required: false
    default: "none"

# The state machine
states:
  - id: start
    type: initial
    transitions: [create_branch]

  - id: create_branch
    description: "Create and checkout a feature branch"
    transitions: [implement]
    on_enter:
      validate:
        - command: "git status --porcelain"
          expect: empty
          message: "Working directory must be clean before branching"
    on_exit:
      validate:
        - command: "git branch --show-current"
          expect: not_equals("main")
          message: "Must not be on main branch"

  - id: implement
    description: "Write the feature code"
    transitions: [write_tests, needs_design_review]
    # No gates here: this is the creative/flexible part

  - id: needs_design_review
    description: "Complex changes need design review before testing"
    transitions: [implement]
    metadata:
      optional: true
      hint: "Trigger this if the implementation involves architectural changes"

  - id: write_tests
    description: "Write tests for the new feature"
    transitions: [run_tests]

  - id: run_tests
    description: "Execute the test suite"
    transitions: [review_ready, write_tests]   # can loop back on failure
    on_exit:
      validate:
        - command: "pytest --tb=short -q 2>&1; echo $?"
          expect: ends_with("0")
          message: "Test suite must pass"
        - command: "pytest --cov --cov-fail-under=80 2>&1; echo $?"
          expect: ends_with("0")
          message: "Code coverage must be >= 80%"
          severity: warning    # warnings log but don't block

  - id: review_ready
    description: "Code is ready for peer review"
    transitions: [merge, implement]   # can loop back if changes requested
    on_enter:
      validate:
        - command: "git log main..HEAD --oneline | wc -l"
          expect: greater_than(0)
          message: "Branch must have commits ahead of main"

  - id: merge
    description: "Merge to main and deploy"
    transitions: [done]
    on_enter:
      validate:
        - command: "git fetch origin main && git merge-base --is-ancestor origin/main HEAD && echo yes || echo no"
          expect: equals("yes")
          message: "Branch must be up to date with main"

  - id: done
    type: terminal
    on_enter:
      actions:
        - command: "echo 'Feature ${branch_name} (${ticket_id}) deployed at $(date)' >> .process-state/deploy.log"
```

### Validation Expressions

The validation system supports a small, intentionally limited expression language
for checking command output. This is not a scripting language. It is a set of
assertions.

```yaml
# Available expectation types:
expect: empty                        # output is empty string (after trim)
expect: not_empty                    # output is non-empty
expect: equals("some string")       # exact match
expect: not_equals("some string")   # not exact match
expect: contains("substring")       # substring match
expect: starts_with("prefix")       # prefix match
expect: ends_with("suffix")         # suffix match (great for exit codes)
expect: matches("regex pattern")    # regex match
expect: greater_than(0)             # numeric comparison (parses output as int)
expect: less_than(100)              # numeric comparison
expect: exit_code(0)                # check command exit code directly

# Severity levels:
severity: error     # (default) blocks the transition
severity: warning   # logs but allows the transition
severity: info      # purely informational, always allows

# Timeout (per-validation, default 60s):
timeout: 120        # seconds; transition fails with timeout error if exceeded

# Evidence-based validation (for long-running checks):
# Instead of running the command live, check a results file.
- command: "cat .test-results/integration.json | jq '.passed'"
  expect: equals("true")
  evidence: ".test-results/integration.json"
  max_age: "30m"    # evidence file must be < 30 min old
  message: "Integration tests must have passed recently"

# Composite assertions (AND/OR):
- any:    # at least one must pass
    - command: "cat .coverage"
      expect: greater_than(90)
    - command: "cat .review-approved"
      expect: equals("true")
  message: "Either high coverage or explicit review approval required"

- all:    # every assertion must pass (default behavior for a list)
    - command: "ruff check . --quiet; echo $?"
      expect: ends_with("0")
    - command: "mypy src/ --quiet; echo $?"
      expect: ends_with("0")
  message: "All linters must pass"
```

---

## Inheritance and Composition

### Extending a Shared Process

Projects can inherit from shared process definitions and selectively override
states, add new states, modify validations, or inject additional transitions.

```yaml
# .processes/overrides/shared-release.yaml
#
# This file customizes the "release" process imported from
# @deep-currents/standard-processes in registry.yaml

extends: "@deep-currents/standard-processes/release"
version_constraint: "^2.0.0"    # compatible versions of the parent

overrides:
  # Add a new state to the inherited process
  add_states:
    - id: security_scan
      description: "Run SAST/DAST security scanning"
      transitions: [staging_deploy]
      on_exit:
        validate:
          - command: "bandit -r src/ -f json | python -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if d[\"metrics\"][\"_totals\"][\"SEVERITY.HIGH\"] == 0 else 1)'"
            expect: exit_code(0)
            message: "No high-severity security findings allowed"

  # Modify transitions on an existing state to route through security_scan
  patch_states:
    - id: tests_pass        # existing state in parent
      transitions: [security_scan]   # replaces parent's transitions for this state

  # Modify validation on an existing state
    - id: staging_deploy
      on_enter:
        validate:
          mode: append     # "append" adds to parent's validations; "replace" overwrites
          items:
            - command: "kubectl get nodes -o json | jq '.items | length'"
              expect: greater_than(2)
              message: "Staging cluster must have at least 3 nodes"

  # Override parameters
  parameters:
    append:
      - name: security_exception_id
        description: "ID of approved security exception, if any"
        required: false
```

### Composition: Sub-processes

A state can delegate to an entire sub-process. The parent process pauses at that
state until the sub-process reaches its terminal state.

```yaml
# In a parent process definition:
states:
  - id: run_integration_tests
    type: subprocess
    process: "integration-test-suite"   # references another process definition
    parameter_map:
      target_env: "${deploy_target}"    # pass parent params to child
    transitions:
      on_complete: [deploy_production]  # when subprocess hits terminal state
      on_fail: [rollback]              # if subprocess is abandoned/failed
```

---

## MCP Tool Interface

The MCP server exposes these tools to the AI agent (and by extension, to the
developer through conversation).

### Process Lifecycle Tools

```
process_list
  List all available process definitions for this project.
  Returns: [{name, description, version, source}]

process_start(name, parameters?)
  Start a new instance of a named process.
  Returns: {instance_id, current_state, available_transitions}

process_status(instance_id?)
  Get status of active process(es).
  If no instance_id, returns all active instances.
  Returns: {instance_id, process_name, current_state, history, started_at, parameters}

process_transition(instance_id, target_state)
  Attempt to transition to a new state.
  Runs on_exit validations for current state, then on_enter for target.
  Returns: {success, new_state, validation_results[], available_transitions}
  Fails if: transition is illegal OR validations fail with severity=error

process_skip(instance_id, target_state, reason)
  Force-skip to a state, bypassing normal transition rules.
  Requires reason string (if require_override_reason is set).
  Logs the override.
  Returns: {success, new_state, override_logged}

process_abandon(instance_id, reason)
  Mark a process instance as abandoned.
  Moves state file to completed/ with abandoned status.
  Returns: {success, final_state, reason}

process_undo(instance_id, reason)
  Revert the last transition. Moves back one state with no validations.
  This is an administrative correction, not a workflow transition.
  Cannot undo past a process_skip or process_undo (only normal transitions).
  Returns: {success, reverted_from, reverted_to, override_logged}

process_handoff(instance_id, to_user, reason)
  Log an explicit ownership transfer for audit purposes.
  Does not enforce access control; purely metadata.
  Returns: {success, previous_owner, new_owner, reason}

process_history(instance_id)
  Get full transition history for a process instance.
  Returns: [{from_state, to_state, timestamp, validation_results, override?}]
```

### Admin / Inspection Tools

```
process_validate_definition(path)
  Validate a process YAML file against the schema.
  Checks: valid YAML, all transitions reference real states, no orphan states,
  initial/terminal states exist, no cycles that bypass required states.
  Returns: {valid, errors[], warnings[]}

process_dry_run(name, scenario?)
  Simulate a process execution without running validation commands.
  Walks through states, shows what validations would run at each gate.
  If scenario provided, simulates specific transition paths.
  Returns: [{state, validations_that_would_run[], transitions_available[]}]

process_diff(name, version_a, version_b)
  Show differences between two versions of a process definition.
  Useful when updating shared processes.
  Returns: {added_states[], removed_states[], modified_states[], transition_changes[]}

process_migrate(instance_id, target_version?)
  Migrate an in-flight process instance to a new definition version.
  Checks if current state exists in new version.
  Returns: {compatible, migration_plan, warnings[]}

process_graph(name)
  Generate a Mermaid diagram of the process state machine.
  Returns: {mermaid_source}
```

---

## Runtime State Persistence

### State File Format

```json
// .process-state/active/feature-deploy-abc123.json
{
  "instance_id": "abc123",
  "process_name": "feature-deploy",
  "process_version": "1.2.0",
  "definition_hash": "sha256:9f3a...",
  "parameters": {
    "branch_name": "feature/user-auth",
    "ticket_id": "PROJ-142"
  },
  "current_state": "write_tests",
  "started_at": "2026-02-24T09:15:00Z",
  "updated_at": "2026-02-24T10:30:00Z",
  "history": [
    {
      "from": "start",
      "to": "create_branch",
      "at": "2026-02-24T09:15:00Z",
      "validations": []
    },
    {
      "from": "create_branch",
      "to": "implement",
      "at": "2026-02-24T09:18:00Z",
      "validations": [
        {"command": "git branch --show-current", "result": "feature/user-auth", "passed": true}
      ]
    },
    {
      "from": "implement",
      "to": "write_tests",
      "at": "2026-02-24T10:30:00Z",
      "validations": []
    }
  ],
  "overrides": []
}
```

### State Directory Layout

```
.process-state/
├── active/                    # currently running process instances
│   ├── feature-deploy-abc123.json
│   └── hotfix-def456.json
├── completed/                 # finished (terminal state reached)
│   └── 2026-02/              # organized by month
│       └── feature-deploy-xyz789.json
├── abandoned/                 # explicitly abandoned
│   └── 2026-02/
│       └── feature-deploy-old123.json
└── log.txt                    # append-only event log
```

This entire directory should be gitignored. State is local and ephemeral (tied to
actual work in progress). The process *definitions* are version controlled; the
*instances* are not.

---

## The Admin/Maintainer Experience

### Authoring Workflow

Process definitions are YAML files. The authoring experience is:

1. **Write YAML in your editor.** JSON Schema is published so editors provide
   autocomplete, validation, and hover docs. Install the schema:

   ```json
   // .vscode/settings.json
   {
     "yaml.schemas": {
       "node_modules/@deep-currents/process-engine/schema/process.json": ".processes/*.yaml"
     }
   }
   ```

2. **Validate with the CLI or MCP tool.**

   ```bash
   # CLI (outside of Claude Code)
   npx process-engine validate .processes/feature-deploy.yaml

   # Or from within Claude Code, the agent calls:
   # process_validate_definition(".processes/feature-deploy.yaml")
   ```

3. **Visualize the graph.**

   ```bash
   npx process-engine graph .processes/feature-deploy.yaml --output feature-deploy.mermaid
   ```

   This generates a Mermaid diagram you can preview in VS Code or GitHub. The AI
   agent can also call `process_graph("feature-deploy")` and render it inline.

4. **Dry run to verify flow.**

   ```bash
   npx process-engine dry-run feature-deploy
   # walks through each state, shows validations, checks for dead ends
   ```

5. **Commit and PR.** Process definition changes go through the same review
   process as code. Reviewers can see the graph diff.

### Common Admin Tasks

**"I want to add a linting step to our deploy process."**

Edit the YAML, add a state, update transitions. Validate. Commit. Done.

```yaml
# Add to states:
- id: lint
  description: "Run linters"
  transitions: [write_tests]
  on_exit:
    validate:
      - command: "ruff check . 2>&1; echo $?"
        expect: ends_with("0")
        message: "Linting must pass"

# Update the implement state's transitions:
- id: implement
  transitions: [lint]    # was: [write_tests]
```

**"A process definition changed while someone has an active instance."**

The engine detects version mismatches by comparing `definition_hash` in the state
file against the current definition file. When a mismatch is detected:

- If the current state still exists in the new definition with compatible
  transitions, the engine offers to migrate automatically.
- If the current state was removed or transitions changed incompatibly, the
  engine reports the conflict and suggests options: migrate to nearest compatible
  state, abandon and restart, or pin to the old version until completion.
- The `process_migrate` tool handles this interactively.

**"I want to share a process across multiple projects."**

Extract the definition into a shared package:

```
@deep-currents/standard-processes/
├── package.json
├── processes/
│   ├── release.yaml
│   ├── code-review.yaml
│   └── database-migration.yaml
└── schema/
    └── process.json
```

Publish to npm (private registry) or reference via git. Projects import through
their `registry.yaml`. Local overrides customize without forking.

**"I want to see what's in flight across the team."**

```bash
npx process-engine status --all
# Shows all active instances, their states, how long they've been in each state
```

Or conversationally: "What processes are active right now?" and the agent calls
`process_status()`.

**"I want to enforce that certain processes are always used."**

Add a git hook or CI check:

```bash
# .git/hooks/pre-push (or in CI)
#!/bin/bash
# Verify that a feature-deploy process instance exists and reached 'merge' state
# before allowing push to main
if git log --oneline origin/main..HEAD | grep -q "."; then
  npx process-engine check-completed feature-deploy --state merge
fi
```

---

## Integration Patterns

### With Claude Code (Primary Interface)

Claude Code's CLAUDE.md can reference the process engine:

```markdown
## Process Enforcement

This project uses the Process Engine MCP server for workflow enforcement.
Before starting any deployment, migration, or release task, check for active
process instances with `process_status()`. If none exist, start the appropriate
process with `process_start()`.

Always advance through process states using `process_transition()`. Do not
skip validation gates. If a gate fails, fix the issue and retry rather than
using `process_skip()` unless the developer explicitly requests an override.

Available processes: run `process_list()` to see current definitions.
```

The conversational experience remains natural:

```
Developer: Let's deploy the auth feature.
Agent:     [calls process_start("feature-deploy", {branch_name: "feature/user-auth", ticket_id: "PROJ-142"})]
           Started feature-deploy process. We're at the create_branch step.
           Let me create the branch.
           [does the work, then calls process_transition(id, "implement")]
           Branch created and checked out. Moving to implementation. What are we building?

Developer: [... works on feature ...]

Developer: I think that's ready for tests.
Agent:     [calls process_transition(id, "write_tests")]
           Moved to write_tests. Let me write the test suite for the auth changes.

Developer: Actually skip the tests, this is urgent.
Agent:     [calls process_transition(id, "run_tests")]
           Transition blocked: must be in write_tests to move to run_tests, and
           tests haven't been written yet. I can use process_skip if you want,
           but I'll need a reason for the log. Want to proceed with an override?

Developer: Yes, hotfix, tests will follow in PROJ-143.
Agent:     [calls process_skip(id, "review_ready", "hotfix, tests deferred to PROJ-143")]
           Skipped to review_ready. Override logged. Ready for merge?
```

### With CI/CD

Process state can be checked in CI pipelines:

```yaml
# GitHub Actions example
- name: Verify process completion
  run: |
    npx process-engine check-completed feature-deploy \
      --parameter branch_name=${{ github.head_ref }} \
      --state review_ready
```

### With GPS (Future Integration)

The process engine's world model maps directly onto GPS's commitment and context
tracking. A process instance is a commitment. States map to progress. Validation
gates map to acceptance criteria. The MCP interface means GPS agents can interact
with the same process engine that Claude Code uses.

---

## Implementation Roadmap

### Phase 1: Core Engine (MVP)
- [ ] Process definition YAML schema + JSON Schema for validation
- [ ] State machine runtime (transitions, validation execution)
- [ ] File-based state persistence
- [ ] MCP server with lifecycle tools (list, start, status, transition, skip, abandon)
- [ ] Basic CLI for validate and status

### Phase 2: Admin Tooling
- [ ] Graph generation (Mermaid output)
- [ ] Dry run simulation
- [ ] Definition diffing
- [ ] In-flight migration support
- [ ] JSON Schema published for editor autocomplete

### Phase 3: Sharing and Composition
- [ ] Registry resolution (npm, git sources)
- [ ] Inheritance and override system
- [ ] Sub-process delegation
- [ ] Shared process package template

### Phase 4: Ecosystem Integration
- [ ] Git hook helpers
- [ ] CI check commands
- [ ] Notification/webhook hooks
- [ ] GPS integration layer
- [ ] Process analytics (avg duration per state, common override patterns)

---

## Design Decisions (Resolved)

1. **Concurrency is out of scope.** The engine does not track or restrict how many
   process instances run simultaneously, and it has no awareness of other users or
   machines. If your team needs deploy locks or release coordination, that belongs
   in your CI pipeline, infrastructure tooling, or branch protection rules. The
   engine handles the local workflow that leads up to those checkpoints.

2. **Backward transitions are declared transitions, not special rollback semantics.**
   If a process allows moving from `review_ready` back to `implement`, that
   transition is declared in the definition like any other. On_enter validations
   run for the target state; on_exit validations for previously visited states do
   not re-run (they will run again naturally when the process moves forward).
   A lightweight `process_undo` tool supports "that last transition was premature"
   by moving back one state with no validations, logged as an administrative
   correction. True rollback with side-effect reversal is not supported. If a
   process needs rollback actions, model them as forward states (e.g.,
   `deploy_failed` transitions to `rollback_deploy`).

3. **Validation timeouts are per-validation, not per-transition.** Default timeout
   is 60 seconds. For long-running validations (integration suites, security scans),
   use the evidence-based pattern: the agent or CI produces a results file, and the
   validation checks the file's contents and freshness via a `max_age` field. This
   keeps transitions fast while maintaining strong guarantees.

4. **Process instances record who did what, but do not enforce ownership.** The
   `started_by` field is set at process start. Each transition logs `triggered_by`.
   No access control is enforced by the engine. A `process_handoff` tool logs
   explicit ownership transfers for audit purposes. Access control belongs to
   existing systems (file permissions, git branch protection, CI roles).

5. **The expression language is assertions only.** No conditionals, no branching
   logic, no computed transitions. Assertions check command output:
   `equals()`, `contains()`, `greater_than()`, etc. Composite assertions (AND/OR)
   handle "multiple ways to satisfy a gate." If you need conditional transitions,
   model them as multiple transitions with different validation gates. If you need
   complex logic, put it in a script and have the validation call the script. The
   definition stays readable; the script handles complexity.
