# Strategy

> Nothing enters your project except through the turnstile.

This document is the product strategy. [Vision](vision.md) says what
Turnstile believes; this says what Turnstile promises, to whom, and in
what order we build it. The threat model behind the central promise is
[guarantee.md](guarantee.md). It was produced through the project's
own `decision-record` process.

## The problem, stated plainly

Every mechanism people use today to make an AI agent follow a workflow
is a suggestion. CLAUDE.md files, rules files, skills, spec documents,
plan files — the agent is *asked* to comply, and over long contexts,
compactions, and handoffs, compliance decays. The practical consequence
is that a human cannot stop watching: skip one session of vigilance and
some important step silently doesn't happen, and the project keeps
building on top of the gap.

The operator's requirement is not "the agent behaves." It is:

**"I can know, without a doubt, that my workflow was followed — without
watching."**

## The claim

Turnstile does not promise agent behavior. It guarantees **work
acceptance**: unverified work cannot become part of the project.

This reframing is the whole strategy. There are only two hard
enforcement architectures in existence today: *own the agent's
execution loop* (LangGraph, Temporal — total control, total rebuild,
useless with agents you don't own) or *intercept tool calls* (runtime
hooks — deterministic but stateless). Neither solves the problem for
people driving off-the-shelf agents. Turnstile occupies the third
position: durable workflow state that interception points consult
in-flight, plus a deterministic conformance check at the **acceptance
boundary** — git hooks, required CI checks, branch protection — where
the model has no reach.

The agent can ignore every suggestion mid-flight. It cannot make its
work *land* without the ledger showing the workflow was followed.

## The trust model: three layers, honest at each

| Layer | Mechanism | Promise |
|-------|-----------|---------|
| **Suggest** (monitor) | Session hooks, warnings grounded in real engine state | The agent is reminded of where it actually is — not a stale markdown wish |
| **Block** (enforce) | Tool-call interception (PreToolUse guard; expand to Bash/git/deploy) | Illegal actions are stopped where the runtime allows interception. Coverage has seams; that's acceptable |
| **Guarantee** (accept) | Deterministic conformance verification at merge/release choke points | Work that did not follow the process does not land. No seams — this layer is outside the model's influence |

Layers 1–2 reduce wasted work. Layer 3 is the product's kernel promise.
We never conflate them in messaging: the guarantee is at acceptance.

## The three pillars that make the guarantee airtight

1. **Tamper-evident ledger.** The instance history and log are
   hash-chained so the trail itself cannot be quietly rewritten. "The
   agent edited the ledger" must not be a hole in "without a doubt."

2. **Reproducible verification.** Because gates *render* truth (run a
   command, check reality) rather than *record* claims, the acceptance
   check can re-execute critical gates instead of trusting stored
   results. Turnstile's audit trail is the only one in this space that
   is reproducible. This is the moat render-don't-record buys us.

3. **Explicit exception policy.** Skips and overrides stay fail-open
   in flight (adoption demands it) but become **named exceptions**
   evaluated at acceptance: each process declares which gates are
   skippable, and which overrides require a human signal. A bypass is
   never silent; it is a visible, attributable decision.

The embodiment of all three is one command:

```
turnstile verify [--process <name>] [--policy <file>]
```

Exit 0: the workflow was followed (required states reached, required
gates passed or re-passed, exceptions authorized). Exit 1: it wasn't,
with a human-readable account of exactly where. Wire that into a
required CI check and branch protection, and the guarantee is
mechanical. `turnstile verify` *is* the product; everything else is
supporting infrastructure.

## Positioning

**One-liner: "Your CLAUDE.md, but enforced."**

- **vs. observability (LangSmith, Langfuse):** they record what
  happened; Turnstile verifies it matched declared intent. Traces have
  no concept of a required step. Complementary — emit transitions as
  spans, never compete.
- **vs. orchestration (LangGraph, Temporal):** they enforce by owning
  the loop; Turnstile enforces without owning the loop, so it works
  with agents you don't control. Never become an orchestrator.
- **vs. CI:** CI verifies the artifact's end state; Turnstile verifies
  the process that produced it — and uses CI as its enforcement point
  rather than replacing it.
- **Category:** workflow conformance for AI agents. Not observability,
  not orchestration, not a framework.

## Roadmap

**Phase 1 — make the guarantee airtight** (engine work; the threat
model driving every item is [guarantee.md](guarantee.md))

1. ✅ `turnstile verify` with per-process acceptance policy (required
   states, required gates, re-run gates, allowed exceptions).
2. ✅ Gate re-execution at verify time; gates classified re-runnable
   vs. advisory and reported as PROVEN vs. ATTESTED.
3. ✅ Artifact binding: the engine records the git SHA on every
   history entry; `verify` checks commit-range coverage — the trail
   certifies *this diff*, not just that *a process ran*.
4. ✅ Authenticated human signals: `turnstile approve` (operator-
   signed CLI) and the Action's `require-pr-approval` (GitHub
   identity); an agent-delivered signal on a human-gated wait state
   fails verification.
5. ✅ Hash-chained ledger with contemporaneous external anchoring
   (engine-side; file and command backends; reference append-only
   anchor server in `ops/anchor_server.py`).
6. ✅ `turnstile doctor`: verify the acceptance boundary itself
   (anchor/key placement, ledger visibility, branch protection where
   queryable).
7. ✅ Widen guard interception: state permissions govern shell
   commands (`run`, `allow_commands`, `deny_commands` with fnmatch
   patterns), not just file edits; the guard routes Bash tool calls
   through the run action.

Phase 1 is complete. All shipped items are experimental surface (see
[STABILITY.md](../STABILITY.md)); usage in
[verification.md](verification.md).

**Phase 2 — put the proof where decisions happen**

5. GitHub Action + PR check/comment rendering the trail: states
   walked, gates passed, exceptions and who authorized them. The
   trust artifact must live where reviewers already look, not in
   `.process-state/`.
6. `turnstile report` — exportable Markdown/HTML conformance record
   (the compliance-evidence story for regulated domains).

**Phase 3 — meet users where they already are**

7. CLAUDE.md migration: a command that extracts the workflow content
   of an existing CLAUDE.md/rules file into a process definition.
   Every "begging markdown" file in the wild is a lead.
8. Definition library: curated, versioned, community-contributable
   catalog of codified workflows (release, incident response,
   security review, data migration). Definitions are the network
   effect; the engine is plumbing.
9. Second runtime adapter, to make agent-genericity an existence
   proof instead of a claim.

Sequencing rationale: the guarantee must be real before it is visible
(1 before 2), and visible before it is marketed broadly (2 before 3).
A PR badge that can be gamed would spend the project's entire trust
budget at once.

## Non-goals (standing decisions)

- **Do not own the loop.** The moment Turnstile schedules agent work,
  it competes with orchestrators and loses its reason to exist.
- **Do not chase perfect in-flight prevention.** Interception coverage
  in runtimes we don't own will always have seams. Acceptance is where
  the promise lives; in-flight blocking is an efficiency feature.
- **Do not become observability.** Integrate outward.
- **Keep the engine small.** Policy, domain knowledge, and convenience
  belong in definitions. (See the [decision filter](vision.md).)

## The success test

One question decides whether any release advanced the strategy:

**"Can the operator stop watching?"**

Concretely: a user starts multi-session agent work, walks away,
returns days later — and can tell in one glance, with mechanical
certainty, that everything merged followed the workflow, and anything
that didn't is visibly quarantined with a named reason. Every feature
either moves toward that or it doesn't ship.

## Reversal conditions

This strategy should be revisited if any of these occur:

- Agent runtimes ship native, durable, cross-session workflow
  enforcement (not suggestions) — the generic wedge narrows; pivot
  fully to the acceptance/audit layer and the definition library.
- Evidence that acceptance-time enforcement is routinely bypassed in
  practice (e.g., teams disable the check when it's inconvenient) —
  the friction model needs rework before wider investment.
- The definition library fails to attract contributions after a real
  push — reconsider whether definitions-as-product holds, and whether
  vertical, domain-specific packaging should replace it.
