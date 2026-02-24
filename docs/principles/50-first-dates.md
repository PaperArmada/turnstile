# 50 First Dates

> Every agent session starts from zero. The only knowledge an agent has of your project or tools is what you make available in context.

**Agents have no persistent memory of your project. If it's not in context, it doesn't exist.**

## Rules

1. **Context is the only interface.** An agent cannot remember what happened last session, what conventions you prefer, or what your codebase looks like. Everything it needs must be surfaced through CLAUDE.md, process definitions, validation gates, or tool responses.

2. **Design for the blank slate.** Every artifact (process definitions, gate commands, error messages, tool descriptions) should be self-explanatory to an agent encountering the project for the first time. If understanding requires prior sessions, the design has failed.

3. **Encode knowledge in durable artifacts, not conversation.** Decisions, conventions, and patterns belong in files (CLAUDE.md, process YAML, config), not in chat history. Chat history is ephemeral; files survive across sessions.

4. **Validation gates are context delivery.** Gates don't just check conditions. They surface information the agent needs at the moment it needs it. A gate that runs `git log --oneline -5` isn't just verifying commits exist; it's teaching the agent what happened recently.

## Anti-patterns

- Expecting an agent to remember a naming convention from yesterday's session
- Writing process definitions that only make sense with background knowledge
- Relying on "the agent will figure it out" instead of explicit guidance in CLAUDE.md
- Error messages that assume familiarity with the system ("Error 42" instead of "No active process found; start one with process_start")

## Application to Turnstile

Process definitions are the primary context delivery mechanism. Each state's description tells the agent what to do. Validation gates surface current project state (git status, test results, file existence) so the agent works from truth, not memory. CLAUDE.md provides the bootstrap: which processes exist, when to use them, and how to start. The enforcement guard's warning message includes the project path so even a fresh agent can diagnose what went wrong.
