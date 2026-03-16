# Multi-Actor Primitives Spike

## Question
What primitives does Turnstile need for multi-actor workflows (human-in-the-loop, specialist agents, role-based state access), and what does building a non-dev process definition reveal about gaps in the current engine?

## Current State

The engine assumes a single anonymous actor per process instance. There is no concept of "who" is performing a transition, no role-based permissions, no way to block until a different actor provides input. Specific gaps:

- **No actor identity.** `transition()`, `skip()`, `abandon()` take no `actor` parameter. `started_by` exists in persistence but is never populated from MCP tools and never enforced.
- **No role model.** `StatePermissions` has `edit` (bool) and `edit_paths` (list), controlling what files can be touched. It doesn't address who can touch them.
- **No waiting primitive.** A state can gate on shell commands, but there's no way to say "block until an external event arrives." Human-in-the-loop requires this.
- **No agent specialization.** `SkillDirectives` suggest Claude Code skills on state entry, but there's no mechanism to configure an agent's role, system prompt, available tools, or MCP servers per state.
- **Subprocess delegation works but is single-actor.** Parent suspends, child runs to completion, parent resumes. The child doesn't know who started the parent or what role it should assume.

## Research: Transferable Primitives from Existing Systems

Surveyed SCXML/Statecharts, BPMN, Temporal.io, XState actors, and multi-agent coordination patterns. The primitives worth adopting fall into four categories.

### Who does the work

BPMN's approach: each task has a `lane` (role) and optionally an `assignee` or `candidateGroups`. User Tasks require human action; Service Tasks are automated. The distinction between "this requires a human" and "this requires an agent with role X" is a first-class concept.

Turnstile equivalent: a `role` field on states, plus a `task_type` discriminator (human / agent / automated).

### How actors communicate

Temporal's signal primitive is the cleanest model: an external system injects an event into a running workflow, which has a handler that updates state and unblocks. This is the human-in-the-loop pattern. The workflow can wait indefinitely because state is durable.

Turnstile equivalent: a state that blocks until an external signal arrives (via MCP tool call, agent mail message, or CLI command).

### How child work is managed

SCXML's `invoke` is polymorphic by type: a child could be another state machine, an HTTP call, or a script. XState distinguishes `invoke` (state-scoped lifecycle, auto-cancelled on exit) from `spawn` (machine-scoped, persists across states). BPMN has escalation as a non-fatal upward signal, distinct from error.

Turnstile already has the invoke pattern via subprocess delegation. What's missing is type polymorphism (the child is always another process definition) and escalation (the child can only complete or be abandoned, it can't send a non-terminal signal upward).

### How waiting and approval work

Temporal: signal (async, fire-and-forget, mutates state), query (sync, read-only), update (sync, validated, mutates state). BPMN: timer boundary events for deadline-based escalation.

Turnstile equivalent: signals for human approval, queries via `process_status`, timeouts as a future extension.

## Proposed Primitives

Five additions, ordered by leverage. Each is designed to compose with existing mechanics rather than replace them.

### 1. Role and session tracking on transitions

Record the **role** (from the target state's declaration) and the **session ID** (from the runtime) on every transition in `HistoryEntry`. When a human acts directly (via CLI or signal), record their identity.

No named agent identity. AI agents lack the temporal continuity and accountability to make named identity meaningful. The role is the functional identity: it says what qualification was active. The session ID provides correlation for debugging and concurrency detection. Human identity is recorded when humans participate directly (approvals, signals, CLI commands), because humans can bear accountability.

**YAML surface:** none. This is engine/MCP plumbing.

### 2. Role declarations on states

A `role` field on `ProcessState` that declares what kind of actor should be in this state. Not enforced at first (like `SkillDirectives`), but surfaced in the MCP response so the calling agent knows whether it should proceed or hand off.

```yaml
states:
  - id: review
    role: reviewer
    description: "Evaluate the deliverable against acceptance criteria"
  - id: implement
    role: developer
    description: "Do the work"
```

**Enforcement spectrum:** Off (advisory, surfaced in response), monitor (warn if actor doesn't match role), enforce (reject transition if role mismatch). Mirrors the existing enforcement model.

**Where roles are defined:** Process-level `roles` list with optional descriptions. Not a global registry. Each process declares its own role vocabulary.

```yaml
roles:
  - id: developer
    description: "Performs implementation work"
  - id: reviewer
    description: "Evaluates deliverables for quality and correctness"
```

### 3. Agent context on states

This is Matt's "specialist agents with specific guidance/tools/prompts at states" idea. A structured block on a state that configures the agent entering it.

```yaml
states:
  - id: legal_review
    role: legal_reviewer
    agent_context:
      system_prompt: "You are reviewing this document for regulatory compliance..."
      tools: ["web_search", "document_reader"]
      guidance: |
        Focus on: data retention, PII handling, third-party sharing.
        Flag anything that requires outside counsel.
```

This extends `SkillDirectives` into something richer. The engine doesn't enforce it (it can't control what the agent loads), but the MCP response surfaces it, and a sufficiently capable agent runtime can configure itself accordingly.

The key distinction from SkillDirectives: skill directives say "run this command." Agent context says "become this specialist." One is an action, the other is a configuration.

### 4. Signal primitive (wait states)

A new state type (alongside `subprocess`): `wait`. A wait state blocks until an external event is received. The event arrives via a new MCP tool (`process_signal`) or CLI command.

```yaml
states:
  - id: awaiting_approval
    type: wait
    role: approver
    signal:
      name: approval_decision
      required_fields:
        - key: approved
          description: "true or false"
        - key: comments
          description: "Reviewer comments"
    transitions:
      - target: approved
        condition: "${signal.approved} == true"
      - target: changes_requested
        condition: "${signal.approved} == false"
```

**Mechanics:** When the engine enters a wait state, the instance is marked as waiting (similar to suspended for subprocesses). `process_signal(instance_id, signal_name, data)` delivers the event, validates required fields, evaluates conditions, and performs the transition.

**Why this matters:** This is the human-in-the-loop primitive. An agent works through states, hits a wait state, and stops. A human (or a different agent) reviews and sends a signal. The process resumes. No polling, no fragile workarounds.

**Integration with agent mail:** A natural fit. When entering a wait state, a notification hook could send a message to the expected actor. When the actor responds, it triggers a signal. This wiring belongs in the notification layer, not the engine.

### 5. Main process (persistent project process)

Not a new primitive per se, but a pattern enabled by the above. A process definition that:

- Runs persistently for a project (started once, rarely reaches terminal)
- Routes incoming work to subprocesses based on type
- Has human-gated transitions at key decision points
- Provides ambient context about project state

```yaml
name: project-main
description: "Persistent project coordination process"

parameters:
  - name: project_name

states:
  - id: ready
    description: "Awaiting new work"
    transitions:
      - target: triage

  - id: triage
    role: lead
    description: "Classify incoming work and route to appropriate subprocess"
    transitions:
      - target: feature_work
      - target: bug_work
      - target: planning
      - target: ready  # nothing to do right now

  - id: planning
    type: subprocess
    process: strategic-decision
    # ...routes back to ready on completion

  - id: feature_work
    type: subprocess
    process: feature-development
    parameter_map:
      feature_name: feature_name
    subprocess_routing:
      on_complete:
        done: review_gate
      on_fail:
        - ready

  - id: review_gate
    type: wait
    role: lead
    signal:
      name: work_review
      required_fields:
        - key: accepted
        - key: feedback
    transitions:
      - target: ready
        condition: "${signal.accepted} == true"
      - target: triage
        condition: "${signal.accepted} == false"

  - id: ready  # cycles back
```

This is where the constraint problem gets addressed. The agent operates freely within a subprocess (feature-development, bug-fix, etc.), but the parent process gates advancement on human review. The judgment call happens at the subprocess boundary, not within the subprocess.

## What a Non-Dev Process Reveals

To test these primitives, consider a **document review** process:

```
draft → internal_review → revision → legal_review → approval → published
```

Observations:

1. **Every state has a natural role.** Author drafts, peer reviews, author revises, legal reviews, executive approves. Single-actor processes can't model this.
2. **Two states require waiting.** `legal_review` and `approval` block until someone outside the process provides input. Shell-command gates can't express this.
3. **Agent context varies by state.** The agent reviewing for legal compliance needs different tools and guidance than the agent drafting content. SkillDirectives are too thin for this.
4. **The validation gates still work.** "Does the document exist?" "Is it longer than 500 words?" "Does it contain required sections?" are all shell-checkable. The mechanical gates compose with the judgment gates rather than replacing them.
5. **Subprocess delegation still works.** If legal review is its own process (with multiple states), it slots in naturally.

The engine's current architecture handles states, transitions, validation gates, and subprocesses. It doesn't handle "who" or "wait for external input." Those two gaps are the blockers for non-dev workflows.

## Implementation Sequence

1. **Role and session tracking** — engine + persistence + MCP. Record role and session_id on transitions, human identity on signals/CLI. Small change, no YAML surface.
2. **Role declarations** — models + YAML surface. Advisory only at first. Test with a non-dev process definition.
3. **Agent context** — models + MCP response. Advisory, surfaced to runtime. Does not require enforcement to be useful.
4. **Signal primitive** — engine + persistence + new MCP tool. The most significant addition. Requires a new state type and the `process_signal` tool.
5. **Main process pattern** — no engine changes needed. It's a process definition that uses subprocesses + wait states + roles. The test case for whether primitives 1-4 compose correctly.

## What to Defer

- **Role enforcement** (blocking transitions on role mismatch). Start advisory, enforce later. Mirrors the existing enforcement spectrum.
- **Parallel regions** (SCXML orthogonal states). Useful but adds significant complexity. Current subprocess model handles sequential delegation; parallel is a Layer 3 concern.
- **Timer/deadline escalation** (BPMN boundary events). Useful for production workflows but not needed to validate the primitives.
- **Claim/unclaim** (BPMN task queuing). Relevant for team workflows; premature for single-user + agents.
- **Spawn** (XState long-lived children). Current invoke model (state-scoped lifecycle) is sufficient. Spawn adds lifecycle complexity without clear near-term value.

## Open Questions (Resolved)

See [multi-actor-conops.md](multi-actor-conops.md) for detailed treatment.

1. **Identity model.** Named agent identity is unnecessary for the engine. AI agents lack temporal continuity and accountability; naming them adds overhead without meaningful auditability. Instead: roles are the functional identity (provisioned via agent_context on states), session IDs provide correlation and concurrency detection, and human identity is recorded when humans act directly (signals, CLI). Agent mail names remain useful as communication addresses for routing work to roles, not as identity.

2. **Signal delivery mechanism.** All three: MCP tool (`process_signal`) as primary, CLI (`turnstile signal`) for human operators, agent mail for cross-role notification. Process definitions should specify notification channel and recipient mechanically (on state entry), removing the judgment call from the agent.

3. **Blocking vs. non-blocking wait.** Wait states suspend the instance, not the agent. The agent's session can end or do other work; the instance sits on disk until a signal arrives. For the main process pattern, this means: dispatch work via async subprocess, return to ready, pick up completed work later. No parallel regions needed; concurrency lives in independent child instances.

4. **Role-to-session routing.** Start with process parameters mapping roles to communication addresses: `process_start("main", {developer_address: "agent-mail-channel", reviewer_address: "..."})`. When the engine enters a dispatch or wait state, it looks up the role's address from parameters and sends the notification mechanically. No named agent registry needed until routing becomes complex.

5. **Main process lifecycle.** Carried forward as open item. Persistent non-terminating processes need adapted analytics and possibly an explicit `shutdown` terminal.

## Async Dispatch (Refinement)

The original proposal assumed sync subprocess delegation for the main process pattern. Discussion revealed a better model: **async dispatch**, where the parent creates a child instance and immediately continues rather than suspending.

Two implementation paths:

**Option A (engine-level dispatch state type):** New `type: dispatch` on states, with `immediate` transition target. Engine creates child, records relationship, transitions parent atomically. Safer; the agent can't get the multi-step sequence wrong.

**Option B (convention over mechanism):** Agent manually calls `process_start` then `process_transition` back to ready, storing child ID in metadata. Simpler to implement; fragile to execute.

Recommendation: prototype with Option B, build Option A once the pattern is validated.

Completed work is tracked via render-don't-record: validation gates on the parent's `ready` state query `turnstile check-completed --parent ${instance_id}` to surface finished children. No mutable dispatch list on the instance. See ConOps for detailed mechanics.

## Mechanical Notification (Refinement)

When a state requires a non-agent actor (wait state with a specific role), the process definition should trigger communication mechanically on state entry, not rely on the agent's judgment:

```yaml
- id: legal_review
  type: wait
  role: legal_reviewer
  signal:
    name: review_complete
  on_enter:
    notify:
      channel: agent_mail
      to: "${legal_reviewer}"
      message: "Review needed for ${document_name}"
```

This extends the existing notification hooks to support per-state notifications. The process author decides the channel and recipient; the engine fires it. The agent entering the state doesn't need to make a judgment call about how to communicate.
