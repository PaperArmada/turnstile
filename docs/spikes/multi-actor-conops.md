# Multi-Actor Concept of Operations

Companion to [multi-actor-primitives.md](multi-actor-primitives.md). This document works through the operational model: how work flows, how agents are identified, and how the system assigns and tracks identity across sessions, processes, and time.

## The construction site model

A multi-actor Turnstile system is like a construction site with specialized equipment. Each piece of equipment has a specific function and maintains its own state. Operators climb in, do work, climb out. What matters is that the operator is qualified for the equipment, not that the same person operates it every time.

### Process instance (the equipment)

A process instance is a piece of equipment on the job site. It has:

- An ID (6-char hex, e.g., `0b7737`)
- A specific function (the process definition: states, transitions, gates)
- A current position (current state)
- An operation log (history of transitions: who, when, from, to)
- A work order (parameters, set at creation, immutable)
- A lifecycle: active → completed or abandoned

An instance persists on disk. It survives sessions, reboots, and operator turnover. It is the durable artifact. When everything else is gone, the instance log tells you what happened.

An instance belongs to a project (the job site), not to an operator. Any qualified operator can interact with any instance.

### Session (the operator)

A session is an operator sitting at the controls. It's a single continuous conversation between a human and an agent runtime (e.g., one Claude Code conversation). It has:

- A session ID (provided by the runtime in hook JSON)
- A start time and an end time
- A conversation transcript (the operator's situational awareness for this shift)
- A set of loaded tools, MCP servers, and configuration

A session is ephemeral. When the operator climbs out, their mental context is gone. The only durable outputs are:

- File changes (code, documents)
- Git commits
- Process instance state changes (transitions, signals)
- Messages sent (agent mail, notifications)

The conversation record (the full transcript, resumable via `/resume`) is the closest thing to a persistent entity. It captures the operator's accumulated context: what they've seen, decided, and done. But it's not identity. It's situational awareness. Two different operators could read the same transcript and act differently based on their configuration.

### Role (the qualification)

A role is a set of qualifications declared on a process state. It answers: "what kind of operator should be at these controls?"

Roles are not persistent entities. They are labels on states that carry configuration. An agent doesn't "have" a role the way a person has a job title. An agent "receives" a role when it enters a state, via the agent_context block that provisions it with the right guidance, tools, and constraints.

This means qualification is provisioned, not verified. The process definition doesn't ask "are you a legal reviewer?" It says "here's how to be a legal reviewer right now." The agent_context block is the qualification mechanism:

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

The role declaration on the state serves three purposes:

1. **Provisioning:** The agent_context configures a general-purpose agent as a specialist for this state.
2. **Routing:** When dispatching work or sending notifications, the engine knows which kind of operator is needed.
3. **Audit:** The transition log records which role was active, for accountability.

Role change typically means session change. There's no mechanism in current agent runtimes to reset context mid-conversation. When work transitions from a developer role to a reviewer role, that's a natural session boundary: the developer session ends (or parks), a reviewer session picks up. Context resets naturally. Within a single role, across multiple states (understand → implement → test), context carries forward within the session.

## How a session learns its role

When a session starts, the agent doesn't inherently know what it is. It gets told, through a combination of:

1. **Process state.** The agent calls `process_status()`, sees active instances, and reads the current state's role and agent_context. The process definition tells the agent what role is needed right now.

2. **SessionStart hook.** Surfaces active instances, pending work, and situational context. Provides immediate awareness of what's happening on the job site.

3. **Human direction.** The operator says "work on the feature-development instance" or "you're reviewing today." This is the simplest and most reliable mechanism in a single-user setup.

The agent doesn't need a persistent identity. It needs to know what seat it's sitting in right now. The process instance and its state provide that.

### What gets recorded in the audit trail

AI agents lack temporal continuity and accountability. They can't be held responsible for decisions. Humans can. The audit trail reflects this:

- **Role:** Recorded on every transition. "A developer did this." The role is the meaningful unit, not the agent's name.
- **Session ID:** Recorded on every transition. Provides correlation (which conversation made which transitions) and concurrency detection (reject if two sessions try to transition simultaneously).
- **Human identity:** Recorded when a human acts directly, such as sending a signal for approval, directing work via CLI, or making a judgment call at a wait state. Humans bear accountability, so their involvement is tracked by name.

Agent mail names (MistyHollow, BlueLake) remain useful for communication routing, but they are not identity in the accountability sense. They are addresses, not signatures.

## How work flows

### Model 1: Synchronous subprocess (current)

This is what Turnstile does today.

```
Main process:  ready → triage → [feature_work] → review_gate → ready
                                      |
                                      v
                               Feature process:  understand → implement → test → done
```

When the main process enters `feature_work` (a subprocess state), the engine:

1. Creates a child instance of `feature-development`
2. Suspends the parent instance
3. The child runs to completion (the same agent works through it)
4. When the child reaches a terminal state, the parent resumes
5. The parent's subprocess routing maps the child's terminal state to the parent's next state

**Everything happens in one session, one agent.** The parent is frozen while the child runs. The agent can't do anything else on the parent until the child is done.

This works for single-agent workflows. It breaks down when:

- The child work should be done by a different agent
- The child work takes days (the parent is frozen the whole time)
- The coordinator wants to dispatch multiple pieces of work

### Model 2: Asynchronous dispatch (proposed)

The main process dispatches work as independent instances, then continues.

```
Main process:  ready → triage → dispatch → ready → ... → ready → review → ready
                                   |                         ^
                                   v                         |
                            Feature instance:  understand → implement → test → done
                            (independent, different agent)        |
                                                                  notify
```

This requires a new concept: **dispatch**. A dispatch state creates a new process instance but does not suspend the parent. The parent immediately transitions to a specified state (typically `ready`).

There are two ways to implement this:

**Option A: Engine-level dispatch state type.**

```yaml
- id: dispatch_feature
  type: dispatch  # new state type
  process: feature-development
  parameter_map:
    feature_name: feature_name
  assign_to: developer  # role or specific agent
  dispatch_routing:
    immediate: ready  # parent goes here right away
    on_complete:       # checked later via gates or signals
      done: review
      abandoned: triage
```

The engine creates the child instance, records the parent-child relationship, and transitions the parent to `ready` in one atomic operation. The child's completion is tracked and surfaces as a signal or gate condition later.

**Option B: Convention over mechanism.**

No new state type. The agent follows a pattern:

1. Agent calls `process_start("feature-development", {feature_name: "..."})` to create the child
2. Agent calls `process_transition(main_id, "ready", metadata={dispatched: child_id})` to return to ready
3. The main process's `ready` state has a validation gate: `turnstile check-completed --dispatched-by ${instance_id}`
4. When the gate surfaces completed instances, the agent transitions to review

**Trade-offs:**

Option A is safer. The engine enforces the pattern. The agent can't forget to record the child ID, can't skip the transition back to ready, can't create the child without linking it. This aligns with Turnstile's philosophy: encode the pattern so the agent doesn't have to get it right by judgment.

Option B is simpler to implement. No new state type, no engine changes. But it relies on the agent following a multi-step convention correctly, which is exactly the kind of thing Turnstile exists to prevent.

**Recommendation: Option A.** The `dispatch` state type is a small addition (similar in scope to `subprocess`), and it eliminates a class of errors. Start with Option B for prototyping, move to Option A when the pattern is validated.

### Model 3: Wait states for external input

When a process needs input from a different actor (human or agent), it enters a wait state.

```
Main process:  ... → review_gate (wait) → ...
                           |
                      instance suspends
                           |
                      notification sent to reviewer
                           |
                      [time passes]
                           |
                      reviewer sends signal
                           |
                      instance resumes, transitions based on signal data
```

The wait state suspends the instance, not the agent. This distinction matters:

- **Instance suspended:** This specific process instance cannot advance until the signal arrives. Its state is `review_gate`, and no transitions are available.
- **Agent free:** The agent's session can end. The agent can start new sessions and do other work. The agent can work on other process instances. The suspended instance just sits on disk, waiting.

When the signal arrives (via `process_signal` MCP tool, CLI command, or agent mail integration), the engine:

1. Validates the signal data against required_fields
2. Evaluates transition conditions against the signal data
3. Performs the transition
4. The instance is active again, with the new state and available transitions

The signal can come from:

- A human, via CLI: `turnstile signal 0b7737 review_complete '{"approved": true}'`
- A different agent, via MCP: `process_signal("0b7737", "review_complete", {"approved": true})`
- An automated system, via agent mail or webhook

### Combining dispatch and wait

The full pattern for multi-actor work:

```
Session A (Coordinator):
  main process: ready → triage → dispatch_feature → ready
  [feature-development instance created, assigned to Developer]
  [session A can continue with other work or end]

Session B (Developer):
  picks up feature-development instance
  works through: understand → implement → test → done
  [instance reaches terminal, notification fires]

Session C (Coordinator, or any agent in lead role):
  main process: ready → review_gate (wait state)
  [notification sent to reviewer/lead]
  [session C ends or does other work]

Session D (Reviewer, or Coordinator):
  reviews the completed work (reads code, runs tests, checks output)
  sends signal: process_signal(main_id, "work_review", {accepted: true})
  main process: review_gate → ready
  [cycle continues]
```

Key observations:

1. **Four sessions, potentially four different agents, one coherent workflow.** The process instance is the thread of continuity. The agents come and go.

2. **No session needs to know the full history.** Each session sees: current state, available transitions, role expectations, and validation gate output. The process definition provides the structure; the instance state provides the position.

3. **The coordinator doesn't babysit.** It dispatches work, returns to ready, and picks up completed work later. It doesn't need to hold context about in-flight work. The instances track that.

4. **The judgment call is contained.** The Developer operates freely within the feature-development process (mechanical gates, clear states). The judgment call (is this work acceptable?) happens at the review_gate wait state, where a qualified actor evaluates the outcome. The Developer never needs to make that judgment; the Coordinator/Reviewer does.

## How the agent knows what to do

When a session starts, the agent needs to answer:

1. What work is available?
2. What should I do next?
3. How should I do it?

### What work is available?

`process_status()` returns all active instances. The SessionStart hook surfaces this automatically. The agent sees: "Main process xyz is in `ready`. Feature instance abc123 is in `implement`."

A proposed enhancement: `process_status` could cross-reference instance states with role declarations, surfacing "instances where the current state matches your role." This is a convenience, not a new primitive.

### What should I do next?

The human directs, or the process definition guides. If the main process is in `ready` and validation gates surface completed dispatched work, the agent knows review is available. If a feature-development instance is in `understand`, the agent knows implementation work is waiting.

### How should I do it?

The agent_context on the current state provides the answer:

```yaml
- id: review_gate
  type: wait
  role: lead
  agent_context:
    guidance: |
      A dispatched subprocess has completed.
      Review the work product, then send a signal to advance.
      If acceptable: process_signal with accepted=true
      If not: process_signal with accepted=false and feedback
```

The agent doesn't need to figure out the protocol. The process definition tells it.

## How roles connect to sessions

Since roles are provisioned (not credentialed), the mapping from "this state needs a reviewer" to "this session is reviewing" is lightweight:

### Single-operator mode (current reality)

One human, one or a few CC sessions. The human directs which session works on which instance. Roles are informational: the agent reads `role: reviewer` and the agent_context, and adjusts its behavior accordingly. No infrastructure needed beyond what exists.

This is where we start. It handles the common case: one person directing one or more agents through a workflow.

### Multi-operator mode (future)

Multiple humans or automated sessions. Roles become routing targets: when a dispatch state assigns work to `role: developer`, the system needs to know where to send the notification. This requires a mapping from role to communication address.

The simplest mechanism: process parameters.

```yaml
parameters:
  - name: developer_address
    description: "Agent mail name for the developer role"
  - name: reviewer_address
    description: "Agent mail name for the reviewer role"
```

When the engine enters a dispatch or wait state, it looks up the role's address from parameters and sends the notification. The receiving session picks up the work and gets provisioned by the agent_context on the state.

No registry-level agent declarations needed until the parameter repetition becomes painful across many process instances.

## Async dispatch: detailed mechanics

### Creating a dispatched instance

When the engine enters a `dispatch` state, it:

1. Creates a new process instance from the specified definition, with parameter_map substitution
2. Records the relationship: child instance ID stored in parent's transition metadata, parent instance ID stored in child's metadata
3. Optionally assigns the child to a role/agent (stored in child's metadata or a new `assigned_to` field)
4. Fires notification to the assigned agent (via on_enter notify hook)
5. Immediately transitions the parent to the `immediate` target state (e.g., `ready`)

Steps 1-5 are one atomic operation from the engine's perspective. The parent never "sits" in the dispatch state.

### Tracking dispatched work

The parent needs to know: what work has been dispatched, and what's its status?

Two approaches:

**Metadata-based tracking:** Each dispatch transition records `{dispatched_instance: "abc123", dispatched_process: "feature-development"}` in the parent's history. A validation gate on the parent's `ready` state queries:

```yaml
on_enter:
  validate:
    - command: "turnstile check-completed --parent ${instance_id}"
      expect: not_empty
      message: "Completed dispatched work awaiting review"
      severity: info
```

This is render-don't-record: the gate computes the current state of dispatched work rather than maintaining a separate list.

**Instance field tracking:** A `dispatched_instances` list on `ProcessInstance` that the engine maintains. Simpler to query, but adds mutable state to the instance.

**Recommendation: Metadata-based.** It follows the render-don't-record principle, uses existing primitives (history metadata + validation gates), and doesn't add new mutable state to the instance model. The `check-completed` CLI command would need a `--parent` flag to filter by parent instance, but that's a small addition.

### What happens when a dispatched child completes

When a dispatched child reaches a terminal state:

1. The engine checks if the child has a parent instance ID in its metadata
2. If yes, it fires a notification to the parent's assigned agent (or the role declared on the parent's current state)
3. It does NOT automatically resume the parent (unlike sync subprocess)

The parent is already in `ready` (or wherever it went after dispatch). The notification surfaces as: "Feature instance abc123 has completed." The coordinator agent, in its next session, sees this via validation gates or inbox messages and decides what to do.

This is intentionally loose coupling. The parent doesn't need to be in a specific state to receive the notification. It processes completed work when it's ready, not when the child finishes.

### Multiple concurrent dispatches

The main process can dispatch multiple pieces of work:

```
ready → triage → dispatch_feature_A → ready → triage → dispatch_bug_B → ready
```

Two child instances are now running independently. The coordinator returns to `ready` after each dispatch. Validation gates on `ready` surface all pending completed work. The coordinator reviews them one at a time:

```
ready → review (feature A) → ready → review (bug B) → ready
```

No parallel regions needed. The main process is sequential. Concurrency lives in the child instances, which are independent. The main process serializes the review/integration of completed work.

This is a deliberate simplification. Full parallel regions (SCXML orthogonal states) would let the main process track multiple in-flight children as concurrent branches. That's more powerful but significantly more complex. The dispatch-and-check model handles the common case (dispatch work, review when done) without introducing parallel state management.

## Communication between agents

Two channels, serving different purposes:

### Structured (process mechanics)

Signals, dispatch notifications, subprocess completion. These are the formal handoffs. "Work is ready for review." "Approval granted." Carried by the engine. Gated by process definitions. Every structured message corresponds to a state transition or a signal delivery.

Structured communication is what gates transitions. If a wait state needs approval, the signal is the structured message that delivers it.

### Unstructured (broadcast feed)

For situational awareness: "I hit an edge case in the auth module, heads up." Not tied to a transition. Not gated. Just information flowing through the system.

Agent mail could serve this as a broadcast feed rather than point-to-point messaging. Agents post observations, and consumption is filtered by relevance. Instead of addressing messages to specific agents, agents mention roles or states, and the receiving session's startup hook pre-filters to show only messages relevant to the current role and active instances.

**Trade-off:** Broadcast builds shared awareness but costs context window space. In a busy project with multiple dispatched processes, the feed could crowd out more useful context (agent_context, validation gate output, work product details). Pre-filtering by role and active instance is essential to keep the signal-to-noise ratio manageable.

### Work product handoff

When context resets between roles (session boundary), how does the new operator know what the previous one did? The conversation transcript is gone. What persists:

- Files on disk (the actual work product)
- Git history (what changed and why)
- Process instance history (states traversed, by whom)
- Validation gates (compute current state of the work product on entry)

Gates bridge the context gap between roles. The reviewer doesn't need the developer's conversation. They need the results. The gate says "here are the files changed, here's the diff, here's the test output." Render-don't-record does real work here: the reviewer's context is "what does the codebase look like right now," not "what did the developer tell me they did."

## Compaction resilience

Agent_context is the qualification mechanism. If a compaction event compresses or drops it, the agent loses its role configuration mid-task. This is a concrete operational risk.

**Re-injection** is the recommended approach (validated in the excel-to-sdk project). When the agent detects context loss or a compaction event occurs, the process engine re-surfaces the current state's agent_context. Possible mechanisms:

- **Post-compaction hook:** If the runtime exposes a compaction event, a hook re-injects the agent_context for the current state via `process_status` or a dedicated `process_context` tool.
- **Periodic re-injection:** The agent periodically checks its process state (e.g., on each tool call via PostToolUse hook) and re-surfaces agent_context if it detects drift.
- **Compact role context with file pointers:** Keep the agent_context block small (a few lines of guidance + pointers to reference files). The detailed domain knowledge lives in files the agent can re-read. The agent_context is the map, not the territory.

The third approach has a compounding benefit: it makes agent_context compaction-safe by default. If the guidance is "read legal_guidelines.md and apply those standards," it doesn't matter if the guidance gets compressed, because the agent can always re-read the file.

**This warrants its own spike** (tracked as `ts-srk`): define the contract between a process engine and an agent runtime about context durability.

## Boundaries

### What separates one process from another?

An instance boundary. Each instance has its own state, its own history, its own parameters. Instances can be related (parent-child via dispatch or subprocess), but they're independent state machines. A child can complete, fail, or be abandoned without corrupting the parent's state.

### What separates one role from another?

Configuration. Each role has an agent_context (guidance, tools, constraints) and a communication address. Roles don't share context: when work transitions from one role to another, that's a session boundary, and context resets. Roles interact through process mechanics (dispatch, signals) and the broadcast feed (agent mail). The engine routes work to roles, not to named agents.

### What separates one session from another?

Time. A session is a single continuous conversation. When it ends, the transcript is gone. The next session starts fresh. Continuity comes from the durable artifacts: process instance state, file changes, git history, agent mail messages.

### What belongs in the engine vs. the process definition?

- **Engine:** State machine mechanics, transition validation, actor recording, signal delivery, dispatch lifecycle, subprocess lifecycle. These are primitives that all processes use.
- **Process definition:** Role assignments, agent context, notification routing, validation gates, state descriptions, transition conditions. These are domain-specific decisions that vary per workflow.
- **Registry:** Agent declarations, role-to-agent mapping, project-wide settings. These are project-level configuration that spans processes.

The engine should not know about "code review" or "legal approval." It should know about "a state with role X, wait type, and signal requirements." The process definition encodes the domain; the engine provides the mechanics.

## Open items carried forward

1. **Dispatch state type implementation.** Start with Option B (convention) to validate the pattern. Build Option A (engine-level dispatch) once the pattern is confirmed.

2. **Signal delivery plumbing.** MCP tool (`process_signal`) is the primary interface. CLI and agent mail adapters follow. The engine receives signals; the delivery mechanism is pluggable.

3. **Notification on state entry.** Extend the existing notification hooks to support per-state notifications (not just lifecycle events). When a wait state is entered, fire a notification mechanically (the process definition specifies channel and recipient, the engine fires it, the agent makes no judgment call).

4. **`check-completed` with parent filtering.** The CLI needs a flag to query completed instances by parent ID. This enables the render-don't-record pattern for tracking dispatched work.

5. **Concurrency guard.** If two sessions try to transition the same instance simultaneously, the engine needs to detect and reject. File-based locking on the instance state file is probably sufficient for the single-machine case.

6. **Main process lifecycle.** A persistent process that cycles through `ready` indefinitely doesn't have a natural terminal state. Options: explicit `shutdown` terminal, or treat it as intentionally non-terminating (with analytics adapted accordingly).

7. **Compaction resilience spike** (`ts-srk`). Define how agent_context survives compaction events. Re-injection is the leading approach. See compaction resilience section above.

8. **Role spec files.** Version-controlled role definitions as standalone files (see below). Provides auditability, reuse across process definitions, and a clear model for what a role is.

9. **Broadcast feed design.** Evaluate agent mail as a broadcast channel with role-based pre-filtering. Need to determine: message format, filtering rules, context budget, and whether this requires agent mail changes or can be built on existing primitives.

## Role spec files

Roles could be defined as standalone version-controlled files rather than inline in process definitions. A role spec is a model that declares:

```yaml
# .roles/legal-reviewer.yaml
name: legal_reviewer
description: "Reviews documents for regulatory compliance"

agent_context:
  system_prompt: "You are reviewing this document for regulatory compliance..."
  tools: ["web_search", "document_reader"]
  guidance: |
    Focus on: data retention, PII handling, third-party sharing.
    Flag anything that requires outside counsel.
  reference_files:
    - docs/legal/compliance-checklist.md
    - docs/legal/data-handling-policy.md
```

Process definitions reference roles by name:

```yaml
states:
  - id: legal_review
    role: legal_reviewer  # resolved from .roles/legal-reviewer.yaml
    type: wait
    signal:
      name: review_complete
```

**What this buys:**

- **Auditability.** Git history shows how a role's definition changed over time. "When did we add PII handling to the legal reviewer's checklist?" is a `git log` query.
- **Reuse.** Multiple process definitions can reference the same role spec. The legal_reviewer role works in a document review process, a contract approval process, and a data migration process without duplicating the agent_context.
- **Separation of concerns.** The process definition says "this state needs a legal reviewer." The role spec says "here's what a legal reviewer is." Process authors and role authors can work independently.
- **Inheritance.** A `senior_legal_reviewer` role could extend `legal_reviewer` with additional guidance or tools, using the same inheritance model that process definitions already support.

This fits naturally alongside `.processes/` as a sibling directory (`.roles/` or within `.processes/roles/`). The registry would resolve role references the same way it resolves process references: local files first, then extended sources.
