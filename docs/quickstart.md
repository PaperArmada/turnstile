# Quick start

Walkthrough: define a process, set up the MCP server, drive a process from an agent.

## 1. Create a process definition

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

## 2. Set up the MCP server

From your project directory:

```bash
uvx --python 3.12 --from "turnstile-cli @ git+https://github.com/PaperArmada/turnstile.git@v0.1.0#subdirectory=packages/turnstile-cli" turnstile init
```

This generates:
- `.mcp.json` — MCP server config using uvx
- `.processes/registry.yaml` — monitor enforcement by default
- `.claude/settings.json` — guard hook
- `.gitignore` entries for machine-specific files

No local clone needed. Restart Claude Code and the process tools are available.

A fresh install in a clean container (one command, no ambient state):

![turnstile init from the pinned v0.1.0 tag: uvx install and the generated files](assets/quickstart.gif)

For turnstile development (live code changes), use dev mode:

```bash
turnstile init --dev
```

## 3. Use the tools

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

For the full set of MCP tools and CLI commands, see [reference.md](reference.md).
