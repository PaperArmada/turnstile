# Quick start

Walkthrough: install the engine, define a process, drive it from an agent. If
you'd rather delegate the whole thing, copy-paste prompts for an agent are at
the [end of this page](#let-an-agent-do-it).

## 1. Install and initialize

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). From your project
directory:

```bash
uvx --python 3.12 --from "turnstile-cli @ git+https://github.com/PaperArmada/turnstile.git@v0.1.2#subdirectory=packages/turnstile-cli" turnstile init
```

This generates:

- `.processes/` — 11 starter process definitions, plus `principles/` (the
  design-principle notes the starters reference)
- `.processes/registry.yaml` — the list of registered processes and engine
  settings. Definitions already present in `.processes/` are registered
  automatically; files that fail to load are reported and skipped.
- `.mcp.json` — MCP server config (uvx, pinned to the release tag)
- `.claude/settings.json` — the Claude Code guard hook
- `.gitignore` entries for the machine-specific files

Init also prints a **Process Enforcement guidance block**: paste it into your
project's `CLAUDE.md` so agents know which process to start before editing.

Two things to know before you restart:

- **Enforcement starts in monitor mode.** After restart, Claude Code receives
  a warning whenever a file is edited without an active process instance.
  Warnings are advisory; nothing is blocked. `turnstile enforce off` removes
  the hook entirely; `turnstile enforce on` upgrades warnings to hard blocks
  of Edit/Write in states that forbid them.
- **The CLI is invoked through the same uvx prefix.** For repeated use, alias
  it:

  ```bash
  alias turnstile='uvx --python 3.12 --from "turnstile-cli @ git+https://github.com/PaperArmada/turnstile.git@v0.1.2#subdirectory=packages/turnstile-cli" turnstile'
  ```

Restart Claude Code and the process tools are available.

A fresh install in a clean container (one command, no ambient state):

![turnstile init from the pinned release tag: uvx install and the generated files](assets/quickstart.gif)

For turnstile development (live code changes), use dev mode:

```bash
turnstile init --dev
```

## 2. Define your own process

Add a YAML file to `.processes/`:

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

**Register it** with one line under `local:` in `.processes/registry.yaml`:

```yaml
local:
- feature-deploy
```

Registration is what makes a process startable: once the registry's `local:`
list is non-empty, files it doesn't mention are ignored. (Files that were in
`.processes/` before `turnstile init` ran are registered for you.)

Check your work:

```bash
turnstile validate .processes/feature-deploy.yaml   # schema and structure
turnstile list                                      # is it registered?
turnstile dry-run feature-deploy                    # simulate the walk
```

Gates run real commands from your project. This example assumes `pytest` is
installed and a `main` branch with commits ahead of it; substitute whatever
your project actually runs. A gate is any shell command plus an expectation on
its output.

## 3. Drive it from an agent

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

## Let an agent do it

Turnstile's operator is usually an agent, so setup can be delegated too. Two
prompts, split by the restart the MCP connection needs.

**Prompt 1 — install** (paste into Claude Code in your project directory):

```text
Set up turnstile (https://github.com/PaperArmada/turnstile) in this project:

1. Run:
   uvx --python 3.12 --from "turnstile-cli @ git+https://github.com/PaperArmada/turnstile.git@v0.1.2#subdirectory=packages/turnstile-cli" turnstile init
2. Add the Process Enforcement guidance block it prints to this project's
   CLAUDE.md.
3. Summarize what was generated and anything the init output warned about.

Then remind me to restart Claude Code so the MCP tools connect.
```

**Prompt 2 — first process** (after the restart):

```text
Using the turnstile MCP tools: list the available processes and pick the one
that fits this task: <describe a small real task>. Start an instance with the
right parameters and advance it state by state as you do the work, showing me
each validation gate's output as you go. Don't skip states; if a gate fails,
show me what it reported and fix the cause rather than working around it.
```
