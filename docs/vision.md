# Vision

> The future of work is process engineering. Turnstile is infrastructure for that future.

## Thesis

AI agents are already capable enough to do real work. What they lack is not ability but trust. Nobody ships agent-driven workflows at scale because the agent can't write code. They don't ship them because they can't explain to their team lead what the agent did, why, and whether it followed the process.

Turnstile is trust infrastructure. It makes agent behavior legible, predictable, and verifiable by encoding human intent as executable process definitions and enforcing them through validation gates that compute ground truth.

## Identity

Turnstile is **agent infrastructure**, not developer tooling.

Developer workflows are the current proving ground, not the ceiling. The underlying machinery has nothing dev-specific about it. A process definition could describe a legal review, a content approval pipeline, a customer onboarding sequence, or an incident response runbook. The engine doesn't care.

The product is not the state machine. The state machine is plumbing. The product is:

1. **The definition format** — codified professional workflows, shareable and version-controlled
2. **The gate system** — validation that checks reality, not self-reports
3. **The role system** — agent context that provisions a general-purpose agent as a specialist on state entry
4. **The signal system** — wait states that block until external input arrives, enabling human-in-the-loop and cross-agent handoff
5. **The enforcement layer** — a trust spectrum from observation to hard enforcement
6. **The interface contract** — a stable surface that any agent runtime can integrate with

## What Turnstile is not

- Not an LLM orchestration framework (LangChain, LangGraph)
- Not a task queue or job scheduler
- Not a project management tool
- Not a CI/CD pipeline
- Not an agent capability layer (it doesn't make agents smarter, it makes them trustworthy)

## Capability layers

Instead of a roadmap, Turnstile evolves through capability layers. Each builds on the previous one. Work at any layer should avoid closing doors on layers above it.

### Layer 0: Single agent, single process, local state
One agent follows one process at a time. State lives on disk. Transitions are validated and logged. The engine is deterministic.

*Status: complete.*

### Layer 1: Composition and sharing
Subprocess delegation, definition inheritance, registry resolution. Processes can be authored once and consumed across projects. Definitions compose into larger workflows.

*Status: complete.*

### Layer 2: Multi-actor awareness
Role-based state provisioning. Wait states with signal delivery for human-in-the-loop gates. Agent context that specializes a general-purpose agent for each state. Handoff between agents (or between agents and humans). Processes that model collaboration, not just solo execution.

The operational model is a construction site: process instances are equipment (persistent, stateful), sessions are operators (ephemeral, interchangeable), and roles are qualifications (provisioned on entry, not credentialed). See [`docs/spikes/multi-actor-conops.md`](spikes/multi-actor-conops.md) for the full concept of operations.

*Status: primitives complete. Async dispatch and main process pattern remain.*

### Layer 3: Organizational process fabric
Cross-project analytics. Definition marketplace. Compliance audit trails. Process definitions as organizational knowledge assets that improve over time through data.

*Status: future.*

## Decision filter

Run proposed features through these questions:

1. **Agent-generic or agent-specific?** Does this serve any agent runtime, or only a particular one? Prefer the generic path. Adapter layers handle specifics.

2. **More portable or less?** Does this make process definitions work in more contexts, or does it couple them to a particular environment?

3. **Primitive or convenience?** Is this a new building block, or a shortcut built on existing blocks? Prefer primitives. Conveniences belong in definitions, not the engine.

4. **Definition or engine?** Could this capability be expressed as a process definition rather than engine code? As the engine matures, bias toward definitions. The engine should be small and stable; definitions are where domain knowledge lives.

5. **Trust-building or capability-building?** Does this help someone trust an agent more, or does it make the agent more capable? Turnstile's job is trust. Capability is someone else's problem.

## Consistency and auditability

Turnstile guarantees **structural consistency**, not **behavioral consistency**.

**Structural consistency** means the agent followed the right steps, in the right order, and passed the right checks. States, transitions, validation gates. These are deterministic, auditable, and environment-independent. A process definition produces the same structure whether it runs in a healthcare project or a fintech project.

**Behavioral consistency** means the agent made the same judgment calls within each state. This is neither achievable nor desirable with LLM agents. The agent's behavior within a state is influenced by the full context stack: project CLAUDE.md, system prompt, memory files, model version, conversation history. A legal reviewer in healthcare *should* behave differently than one in fintech, even following the same review process.

The auditability boundary is the **validation gate**. Gates are deterministic. They check outputs, not reasoning. "Does the document contain a PII section?" doesn't depend on the system prompt. "Did tests pass?" doesn't depend on CLAUDE.md. Render-don't-record ensures gates verify what's true right now, regardless of how the agent got there.

**What Turnstile can audit:**
- Process structure was followed (states, transitions, timing)
- Gates passed (mechanical verification of outputs)
- Role context that was provided (agent_context, version-controlled via role spec files)
- Human involvement at wait states (who signaled, what they approved)

**What the version control system can audit:**
- The full context stack at any point in time (CLAUDE.md, role specs, process definitions)

**What Turnstile does not audit:**
- Agent reasoning within a state (lives in the conversation transcript, outside scope)

For domains requiring tighter behavioral consistency (compliance, regulatory), the process author writes more specific gates and richer role specs. More gates = more checkpoints = more auditability. The process definition controls how tight the constraints are.

Role context (agent_context) is **additive**, not replacement. It layers on top of the project's existing configuration without modifying it. Turnstile never writes to or overwrites a consumer's CLAUDE.md, system prompt, or memory files. The consumer's environment is theirs; Turnstile adds process awareness on top of it.

## Principles

The design principles in [`docs/principles/`](principles/) govern how Turnstile is built. The decision filter above governs what gets built. Both inform every contribution.

The principles that matter most at the strategic level:

- **Portable by Default** — definitions are the distributable unit; they must work everywhere
- **50 First Dates** — agents start from zero; the process definition is the memory
- **Render Don't Record** — gates compute truth; this is what makes trust verifiable, not performative
- **Fail Open** — enforcement earns trust gradually; blocking is a privilege, not a default
