# Compaction Resilience Spike

## Question
How should agent_context (role configuration, guidance, tools) survive compaction events? What is the contract between a process engine and an agent runtime about context durability?

## Findings

### How compaction works in Claude Code

Compaction is automatic and invisible. It triggers when the context window approaches its limit (also available manually via `/compact`). There is no way to disable it.

**What survives:**
- System prompt (core runtime instructions)
- CLAUDE.md instructions (specifically protected)
- User prompts and key conversation landmarks

**What does not survive:**
- Detailed tool output
- Context injected mid-conversation (including agent_context blocks surfaced in MCP responses)
- System-reminder tags in tool results

**Available hooks:**
- `PreCompact` fires before compaction (trigger: "manual" or "auto"), informational only
- `PostCompact` fires after compaction (receives compact_summary), informational only
- Neither hook can re-inject context or modify the compaction result

### The problem

Agent_context is surfaced in the MCP response when an agent transitions to a new state. It's conversation context, not system-level context. Compaction will compress or drop it. If this happens mid-task, the agent loses its role configuration: what it's supposed to focus on, what reference files to consult, what tools to prefer.

### What already works

The enforcement guard surfaces state context on every Edit/Write via the PreToolUse hook. This means on every file mutation, the agent sees:
- Which process instance is active
- What state it's in
- The state description
- Available transitions

This is already a form of continuous re-injection. The agent is reminded of its structural position on every tool call. What's missing is the role-level context: the agent_context guidance, reference files, and tools.

## Recommendation: Extend the guard nudge

The guard already fires on every Edit/Write and surfaces state context. Extend it to include agent_context when present. This is the most natural re-injection point: it requires no new hooks, no new mechanisms, and it's already proven to work.

### Current guard nudge (enforcement.py)

```
Process: Permitted by feature-development @ implement
You are in: feature-development @ implement (instance ef6c90)
  Write the feature code
  Next: test, understand, abandoned
```

### Proposed guard nudge with agent_context

```
Process: Permitted by feature-development @ implement
You are in: feature-development @ implement (instance ef6c90)
  Write the feature code
  Next: test, understand, abandoned
  Role: developer
  Guidance: Focus on implementation. Write tests for new functionality.
    Reference: docs/architecture.md
```

### Why this works

1. **Fires on every mutation.** The agent gets reminded of its role context every time it edits or writes a file. Compaction can't outrun this; the next tool call re-injects.

2. **No new hooks needed.** The PreToolUse guard hook already exists and fires reliably. We're just adding more data to what it surfaces.

3. **Compact by design.** The nudge is a few lines, not a full system prompt. It survives being repeated because it's small. It doesn't compete for context window space.

4. **Layered with CLAUDE.md.** The project's CLAUDE.md already has a "Process Enforcement" section that tells the agent to check process status. This is the durable instruction. The guard nudge is the real-time reminder. Together they cover both the "know to check" and "here's what's current" aspects.

### What to include in the nudge

Keep it compact. The nudge fires on every Edit/Write, so every token counts.

- **Role** (if set): one line
- **Guidance** (if set): first 200 chars or first 3 lines, whichever is shorter. Full guidance available via `process_status` or by reading the process definition.
- **Reference files** (if set): file paths listed, not file contents
- **Tools** (if set): tool names listed

The nudge is a pointer, not the full context. It tells the agent "you're a security reviewer, check docs/security/checklist.md." The agent can re-read the file. The guidance is a reminder, not the complete specification.

### Implementation

Extend `_build_context()` in enforcement.py to include role and agent_context from the state definition (it already loads the state object). Extend `_format_guidance()` to surface them in the nudge text. Truncate guidance to keep the nudge compact.

Changes:
- `EnforcementContext`: add `role: str = ""` and `agent_context_summary: str = ""` fields
- `_build_context()`: populate from state definition
- `_format_guidance()`: include in output when present

### Fallback: process_status as recovery

If the agent detects it's lost context (e.g., after compaction, it doesn't recognize the process it's in), it can call `process_status()` to get the full state info. The MCP response includes role and agent_context. This is the manual recovery path, complementing the automatic guard nudge.

### What to defer

- **PreCompact hook logging.** Useful for observability but not for resilience. The guard nudge handles re-injection without needing to detect compaction.
- **Writing agent_context to CLAUDE.md.** Invasive; modifies the consumer's project files. The guard nudge achieves the same effect without file writes.
- **PostCompact re-injection hook.** Claude Code's PostCompact hook is informational only; it can't inject context into the conversation. The guard nudge sidesteps this limitation by injecting on the next tool call instead.

## Contract: Process engine and agent runtime

The contract between Turnstile and any agent runtime:

1. **The engine surfaces role context on state entry** (via TransitionResult/MCP response). This is the initial provisioning.

2. **The engine re-surfaces role context on every file mutation** (via enforcement guard / PreToolUse hook). This is the continuous reminder that survives compaction.

3. **The engine provides full context on demand** (via process_status MCP tool). This is the manual recovery path.

4. **The runtime is responsible for acting on surfaced context.** The engine can't force the agent to read a reference file or use a specific tool. It can only surface the guidance.

5. **Role context in the nudge is compact and pointer-based.** Full context lives in the process definition and reference files. The nudge points to it; it doesn't duplicate it.

This contract is runtime-agnostic. Any agent system that supports PreToolUse-style hooks (or equivalent interception points on tool calls) can implement the same pattern. The engine provides the data; the runtime provides the injection point.
