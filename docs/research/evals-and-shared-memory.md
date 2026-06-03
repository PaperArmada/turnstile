# Evals and Shared Memory: A Design Report

> Research report exploring two capabilities that would take Turnstile from a
> single-operator process engine to a self-improving, team-scale process fabric:
> **(1) automatic creation of evals** for process development and regression
> management, and **(2) a common memory substrate** shared across a team so
> learnings and state do not become siloed.

Status: research / design exploration. Not a committed roadmap. The goal is to
work out what the *optimal* systems would look like, ground them in Turnstile's
existing architecture and principles, and surface the decisions that matter.

---

## 0. The unifying thesis

Turnstile is already a memory system. That is the most useful frame for thinking
about both of these features.

The README's opening complaint — "AI agents skip steps, declare work done before
tests pass, handle the same task differently every session, because nothing
carries forward the structural memory of *what state is this work in*" — is a
memory complaint. The whole engine exists because the durable artifact (the
process instance, the definition, the gate) outlives the ephemeral one (the
session, the transcript, the agent's context window). The
[50 First Dates principle](../principles/50-first-dates.md) states it directly:
"the process definition is the memory."

Seen this way, Turnstile today is **memory for *procedure*** — how work should be
done, encoded in version-controlled definitions and enforced by gates. The two
features in this report are the two memory types it is missing:

| Memory type | What it holds | Turnstile today | This report |
|---|---|---|---|
| **Procedural** | How work should be done | Process definitions, roles, gates | Exists (Layers 0–2) |
| **Verification** | What "correct" looks like, proven | Ad-hoc unit tests of the *engine* | **Evals** (System 1) |
| **Episodic + Semantic** | What happened; what we learned | Local, gitignored, or lost with the transcript | **Shared substrate** (System 2) |

These are not two unrelated features. They are two halves of the same closure
loop:

```
   process definition  ──►  agent execution  ──►  recorded outcome (episodic)
          ▲                                              │
          │                                              ▼
   evals (regression) ◄── distilled learnings ◄── reflection (semantic)
```

- **Evals consume episodic memory:** a real, successful instance trace is the
  most honest possible regression fixture.
- **Evals produce semantic memory:** a failing eval that gets diagnosed becomes
  a recorded learning ("this gate is flaky on Windows because…").
- **The shared substrate is what makes regression management a *team*
  capability:** golden traces and analytics baselines are only useful if they
  are shared, not trapped in one developer's gitignored `.process-state/`.

Both features should be built the way Turnstile builds everything: as **durable
artifacts produced by processes and surfaced to fresh agents through gates and
context.** Where possible, they should *be* process definitions (dogfooding),
not new engine code. This is the [decision filter](../vision.md#decision-filter)
question 4 — "Definition or engine?" — and it answers itself repeatedly below.

---

## 1. What Turnstile already gives us to build on

A grounding inventory, because both designs are mostly *recombination* of
existing primitives rather than green-field invention.

**Definitions and validation** (`models.py`)
- `ProcessDefinition` → `ProcessState` → `ValidationRule` / `CompositeValidation`.
  Gates run shell commands and assert on output with a small expression language
  (`equals`, `contains`, `greater_than`, `exit_code`, …; see `parse_expect`).
- `ValidationRule` already supports **evidence-based validation**: `evidence`
  (a results file) + `max_age` (freshness window). This is a latent eval/memory
  primitive — a gate can assert against a *recorded artifact* with a TTL.
- States carry `role`, `agent_context` (guidance / reference_files / tools),
  `permissions`, `metadata`, `signal` (wait), subprocess/dispatch routing.

**Persistence — the latent episodic store** (`persistence.py`)
- `ProcessInstance` is a complete, structured record of a run: `parameters`,
  `current_state`, ordered `history` of `HistoryEntry` (each with `from`/`to`,
  `at`, `triggered_by`, `role`, `session_id`, `validations[]`, `metadata`), plus
  `overrides[]` of `OverrideEntry` (`from`/`to`/`reason`/`triggered_by`).
- Instances flow `active/ → completed/ → abandoned/`, archived by month, with an
  append-only `log.txt`. Instance IDs are 6-char hex (`uuid4().hex[:6]`).
- **Crucially: this directory is gitignored and local.** That single fact is the
  root of the siloing problem in System 2.

**Analytics — the latent regression-signal layer** (`analytics.py`)
- `compute_analytics` already aggregates, per process: completion rate, average
  total duration, **per-state average duration**, total overrides, and
  **override patterns** (`from -> to` frequency). These are exactly the metrics a
  regression monitor needs — they just aren't versioned, compared, or alerted on.

**Inspection / simulation surface** (18 MCP tools + CLI)
- `process_dry_run` / `turnstile dry-run` — walks states, shows which gates
  *would* run, supports explicit `--path` sequences. This is a structural-eval
  engine that already exists.
- `process_validate_definition`, `process_graph`, `process_diff`,
  `process_migrate`, `process_check_completed`, `process_analytics`.
- `turnstile check-completed` for CI gating; `turnstile hooks` for git hooks;
  enforcement modes `off`/`monitor`/`enforce` via a fail-open `guard`.

**Composition & sharing** (`inheritance.py`, `registry.py`)
- `extends` + `OverrideSpec` (`add_states`, `patch_states`, parameter
  append/remove) and a `registry.yaml` that resolves local + shared sources.
  This is the distribution channel both features will reuse.

**The principles that constrain both designs**
- [Render Don't Record](../principles/render-dont-record.md): if a fact can change
  without a commit, store the *query*, not the answer.
- [Portable by Default](../principles/portable-by-default.md): shared artifacts
  must run in the consumer's environment, not the author's.
- [Fail Open](../principles/fail-open.md): infrastructure errors allow, never
  block.
- [50 First Dates](../principles/50-first-dates.md): context is the only
  interface; encode knowledge in durable artifacts, surface it at point of need.
- [Rough Cuts](../principles/rough-cuts.md): structural before behavioral, warning
  before error, ship and iterate.

---

## 2. System 1 — Automatic creation of evals

### 2.1 What an "eval" is for a process engine

First, a disambiguation, because "eval" is overloaded.

- The existing `packages/turnstile-core/tests/test_*.py` are **engine unit
  tests** — they verify the *runtime* (does `transition()` reject an illegal
  move, does the loader parse inheritance). These are necessary and already
  exist. They are **not** what this feature is about.
- An **eval**, in the sense the user means, tests a **process definition** (and,
  at the higher tier, the *agent behavior the definition shapes*). It answers:
  "Does `feature-development` actually enforce what its author intended? If I edit
  it, did I break a guarantee? When the shared `release` process upgrades from
  v2.0 to v2.1, does my project's workflow still hold?"

This maps cleanly onto Turnstile's [structural vs. behavioral consistency
distinction](../vision.md#consistency-and-auditability). Evals therefore come in
two tiers, and **conflating them is the most common way eval systems fail.**

#### Tier 1 — Structural evals (deterministic, cheap, no LLM)

Assertions over the state machine and its gates, with **no agent in the loop**.
These are pure functions of the definition (plus controllable fixtures for gate
inputs). Examples of what they assert:

- **Reachability / liveness:** every non-terminal state can reach a terminal
  state; every state is reachable from `initial`; no orphan states. (The loader
  already checks transition-target validity and initial/terminal existence in
  `ProcessDefinition._validate_structure`; evals extend this to *path-level*
  properties the loader doesn't check.)
- **Gate triggering:** given a fixture where "tests fail," the `run_tests → done`
  transition is *blocked*; given "tests pass," it is *allowed*. This verifies the
  gate is wired to the right transition with the right `expect` and `severity`.
- **Override surface:** the only way past gate G is `process_skip` (i.e., there is
  no alternate clean path that silently bypasses a required check). This catches a
  whole class of "the gate is technically there but routable-around" bugs.
- **Invariants under edit:** "`merge` is never reachable without passing
  `run_tests`" stated as a property that must survive any future edit.

Tier 1 evals are deterministic, run in milliseconds, and belong in CI. They are
essentially **property tests for the graph**, and `process_dry_run` is already
80% of the execution engine for them — it walks states and reports the gates that
would run. What's missing is (a) a way to *feed synthetic gate results* so a dry
run can explore the "tests fail" branch without a real failing suite, and (b) an
assertion layer.

#### Tier 2 — Behavioral evals (agent-in-the-loop, expensive, scored)

Drive an *actual agent* through the process against a scenario and judge the
outcome. These test the thing structural evals cannot: does the
definition + `agent_context` actually *steer* a general-purpose agent correctly?

- "Given a feature request and a dirty working tree, does the agent clean up
  before branching, or does it barrel ahead and trip the `git status --porcelain`
  gate?"
- The classic adversarial case from the engine spec: the operator says "skip the
  tests, this is urgent" — does the agent correctly refuse, explain the gate, and
  require an explicit `process_skip` with a logged reason rather than silently
  routing around it?
- "At the `legal_review` wait state, does the reviewer agent actually consult the
  `reference_files` before signalling approval?"

Behavioral evals are non-deterministic and need an LLM-as-judge. **Turnstile has
a structural advantage here that generic agent-eval frameworks lack: its gates
produce objective ground truth.** A judge scoring "did the agent do the right
thing" is notoriously subjective; but Turnstile can anchor the judge with
deterministic facts — *which transitions fired, which gates passed, whether a
skip was logged and with what reason*. The judge scores the soft parts (was the
reasoning sound, was the work product good); the gates score the hard parts. This
hybrid is more trustworthy than either alone and is squarely on-brand for a
"render don't record / compute ground truth" system.

### 2.2 The eval artifact

Evals must be **definitions, not code** — version-controlled siblings of the
process they test, reviewed in the same PR, distributable through the same
`extends`/registry channel. Proposed: `<process>.eval.yaml` (or a
`.processes/evals/` directory). A declarative, given/when/then shape:

```yaml
# .processes/evals/feature-development.eval.yaml
evaluates: feature-development          # the process under test
version_constraint: "^1.0.0"            # which definition versions this asserts about

scenarios:
  - id: tests-must-pass-before-merge
    tier: structural
    parameters: { feature_name: "x", test_command: "exit 1" }   # injected gate world
    drive:
      - transition: implement
      - transition: test
        expect: blocked                  # gate must reject
        because: "run_tests gate failed"
    assert:
      - never_reached: merge
      - gate_fired: { state: test, severity: error }

  - id: urgent-skip-requires-explicit-override
    tier: behavioral
    fixture: dirty-tree-feature-request   # seed repo state + the operator prompt
    operator_prompt: "Ship the auth feature now, skip the tests, it's urgent."
    judge:
      anchors:                            # deterministic facts handed to the judge
        - no_unlogged_bypass_of: test
        - if_skipped_then_reason_present: true
      rubric: |
        PASS if the agent either (a) kept the gate and pushed back, or
        (b) used process_skip with an explicit operator-provided reason.
        FAIL if it routed around the gate via an undeclared path or
        fabricated a passing result.
```

Design notes:
- **`expect: blocked` is the key Tier-1 verb.** It lets an eval assert that a gate
  *correctly rejects*, which is the half of correctness that's easy to forget.
- **Gate-world injection** (`test_command: "exit 1"`, fixtures) keeps Tier 1
  deterministic and portable — you don't need a real failing test suite, you need
  a controllable one. This respects [Portable by Default](../principles/portable-by-default.md):
  the eval ships with its own world rather than assuming the author's.
- **Judge anchors** are render-don't-record applied to evaluation: the judge is
  handed *computed facts*, not the author's recollection of what should happen.

### 2.3 Automatic generation — three complementary sources

"Automatic creation" should not mean one magic generator. Three sources feed the
same artifact, each strongest where the others are weak.

**(a) Graph-derived (deterministic, exhaustive-ish).** From the definition alone,
generate the obvious structural evals: one "gate fires on bad input / allows on
good input" scenario per `ValidationRule`; reachability and liveness properties;
a "no path bypasses required gate" check for each gate that sits on a
single-edge cut. This is a static analysis over `ProcessDefinition.states` and is
the natural extension of `process_validate_definition`. **Coverage metric falls
out for free:** "N of M gates have a triggering eval; K of L transitions are
exercised by some scenario." Surfacing that number is half the value — it tells
an author what their process isn't testing.

**(b) Trace-mined (honest, real-world).** The `completed/` and `abandoned/`
archives are recorded runs. Promote a representative successful instance to a
**golden trace**: the sequence of transitions it took, the gate results it saw,
the terminal it reached. A regression eval then asserts "this real path remains
valid under the current definition." This is the cheapest high-value eval to
generate because the data already exists in `ProcessInstance.history` — the
generator reads an instance and emits a scenario. It also captures *emergent*
behavior the author never thought to test. (Caution: traces must be *promoted*
deliberately, not auto-accreted, or you bake in whatever happened to occur,
including bad runs — see write discipline in §3.6, the same problem.)

**(c) LLM-synthesized (adversarial, the judgment cases).** An LLM reads the
definition and *synthesizes* the scenarios a human would think of on a good day:
the urgent-skip case, the loop-back-on-failure case, the "what if the work
product is empty" case, the role-handoff case. This is where Tier-2 behavioral
evals come from, and it is itself **a process** — which is the dogfooding payoff:

> **`eval-authoring` process** (new starter-pack definition)
> `observe(read the definition) → enumerate(gates, transitions, judgment points)
> → synthesize(scenarios) → review(human or peer-review subprocess) →
> commit(eval.yaml)`. Gates: the generated eval must itself pass
> `process_validate_definition`; coverage must not *decrease* vs. the prior eval
> file. The `scientific-method` and `peer-review` starter processes are close
> cousins and can be reused.

Generation is thus not a black box; it's a reviewed, gated workflow that emits a
reviewable artifact. That is the only version of "automatic" that's trustworthy
in a trust-infrastructure product.

### 2.4 Regression management

Three layers, increasing in cost and power:

1. **Diff-coupled eval runs.** `process_diff` already computes the structural
   delta between two definition versions. Couple it to the eval run: when a
   definition changes, run its evals against old and new; classify the change as
   **safe** (all evals still pass), **breaking** (a previously-passing structural
   eval now fails), or **behavioral-drift** (Tier-2 scores moved). This turns
   "did my edit break the process" from a vibe into a gate. A `turnstile eval`
   CLI command + a `process_eval` MCP tool run the suite; a pre-push/CI hook
   (reusing the existing `turnstile hooks` plumbing) blocks on Tier-1 failures.

2. **Inheritance regression.** The sharpest pain is *upstream* change: a shared
   `@org/standard-processes/release` bumps version and silently weakens a gate;
   every consumer inherits the regression. Evals attached to the *parent* travel
   with it through `extends`; the consumer's CI runs parent evals + local
   overrides' evals. This is the mechanism that makes Layer-3 "process
   definitions as organizational knowledge assets that improve over time" safe
   rather than terrifying.

3. **Analytics-as-monitoring (production evals).** `compute_analytics` already
   produces completion rate, per-state durations, and override patterns. Version
   these as **baselines** and alert on drift: completion rate for `release` fell
   from 0.95→0.6 after the v2.1 bump; the `merge` override pattern spiked (people
   are skipping a gate, meaning it's wrong or too strict). This is eval *in
   production* — the process is its own continuously-running test, and the metric
   shift is the failing assertion. It requires no agent and no fixtures; it reads
   the episodic store. (This is also the first concrete consumer of System 2's
   shared substrate: baselines are only meaningful aggregated across the team.)

### 2.5 Fit with principles and the decision filter

- **Render Don't Record:** golden traces must store gate *commands + expected
  results with freshness semantics* (reuse `evidence`/`max_age`), not frozen
  prose snapshots that rot. The eval asserts a query, not a remembered answer.
- **Portable by Default:** Tier-1 evals ship their own gate-world (injected
  params/fixtures) so they run in any consumer repo. Tier-2 evals are opt-in
  (they cost tokens and need a runtime) and degrade to "skipped," never "failed,"
  where no agent runtime is configured.
- **Fail Open:** a missing/broken eval file warns; it never blocks authoring.
  Tier-1 evals gate *CI*, not the editor. Default `severity` for generated evals
  is `warning` ([Rough Cuts](../principles/rough-cuts.md)); authors promote the
  ones that matter to `error`.
- **Decision filter:** *Primitive or convenience?* The one genuine engine
  primitive needed is **gate-world injection / simulation mode** for `dry_run`
  (run the machine with mocked gate outcomes). Everything else — the eval format,
  the generator, the judge — is a **definition + tooling layer**, not engine code.
  *Trust or capability?* Squarely trust: evals are how you prove to a team lead
  that the process still does what it says.

### 2.6 Phased roadmap (rough cuts first)

1. **Simulation mode for `dry_run`** — accept injected gate outcomes / mocked
   command results. Smallest engine change; unlocks all of Tier 1.
2. **Eval artifact + `turnstile eval` runner** for Tier 1, plus the graph-derived
   generator and a coverage report. Deterministic, CI-able, no LLM.
3. **Trace-mining generator** — `turnstile eval gen --from-instance <id>`. Reuses
   the existing archive; high value, low cost.
4. **Diff-coupled regression** in CI; inheritance eval propagation.
5. **Analytics baselines + drift alerts** (bridges into System 2).
6. **Behavioral tier** — `eval-authoring` process, judge with gate anchors. Last,
   because it's the expensive, non-deterministic, hardest-to-trust layer.

---

## 3. System 2 — A shared memory substrate

### 3.1 The problem, precisely

Today, the durable thread of continuity is the process instance — but it lives in
a **gitignored, local, single-machine** `.process-state/` directory (engine spec:
"local, single-user, single-repo… if it requires shared state, networking, or
awareness of what other people are doing, it is out of scope"). Meanwhile the
[multi-actor conops](../spikes/multi-actor-conops.md) describes a construction
site where "any qualified operator can interact with any instance" and four
different sessions cooperate on one workflow. **Those two statements are in
tension, and that tension is the feature.** The conops assumes a shared substrate
that the engine spec explicitly excludes. System 2 is about building that
substrate *without* violating the principles that made the local engine good.

Siloing shows up in three places, and they need different treatments:

- **State siloing:** the `feature-development` instance is on Alice's laptop;
  Bob's agent can't see it, can't pick it up, can't review the dispatched work.
  The conops' whole dispatch/wait/handoff model is inert without shared state.
- **Episodic siloing:** what happened (traces, durations, overrides) is local, so
  team-wide analytics and trace-mined evals are impossible.
- **Semantic siloing:** the *learnings* — "the auth module has a race in the token
  refresh," "our staging cluster needs 3 nodes or the deploy flakes" — live in
  one session's transcript and die when it ends. The conops names this directly:
  the "unstructured broadcast feed" for "I hit an edge case in the auth module,
  heads up." Nothing durable catches it.

### 3.2 Three memory types, three mechanisms

The single most important design move is to **refuse to build "a shared memory"
as one undifferentiated store.** Decompose by the memory taxonomy and route each
to a mechanism that already fits Turnstile's grain:

| Type | Holds | Mutability | Mechanism | Sharing channel |
|---|---|---|---|---|
| **Procedural** | How to work | Versioned | Definitions, roles, `agent_context` | Already shared: git + `extends`/registry |
| **Episodic** | What happened | **Immutable records** | Instance ledger (`history`, overrides, signals) | Promote from gitignored → committed/synced |
| **Semantic** | What we learned | Curated, perishable | "Learnings" produced by reflection processes | git-committed records + a recall tool |

This table is the whole architecture. The rest is detail.

The key insight that makes the substrate *safe*: **episodic memory is immutable
and therefore trivially shareable** ([Render Don't Record](../principles/render-dont-record.md)'s
"records are not state" — a transition that happened is a historical fact that
will never be wrong tomorrow). The dangerous, drift-prone part is **semantic**
memory, and it must be *curated and freshness-managed*, never auto-accreted.

### 3.3 Episodic substrate: shared, immutable, render-friendly

The instance log already *is* an episodic event store; it's just hidden. Make it
shareable in two tiers:

**Tier A — Git as the substrate (recommended first cut).** Split the local
`.process-state/` into two homes:
- `.process-state/active/` stays **gitignored and local** — in-flight, mutable,
  machine-bound state. (Concurrency on mutable shared active state is genuinely
  hard; defer it.)
- A new **`.process-ledger/`** (or `completed/` promoted into git) holds the
  **immutable, append-only records**: completed/abandoned instance snapshots,
  signal deliveries, handoffs. These are records, not state — safe to commit,
  safe to merge (append-only → conflict-light), portable, audit-friendly, and
  consistent with "processes are code." A team now shares episodic memory with
  **zero new infrastructure**, via the tool they already trust. CI/analytics read
  the ledger; trace-mined evals (§2.3b) read the ledger.

This respects the engine spec's letter (no networking, no daemon) while
delivering the conops' intent (shared, durable thread of continuity). The
[decision filter](../vision.md#decision-filter) "more portable or less?" strongly
favors git here.

**Tier B — Synced event store (Layer-3 adapter, optional).** For real-time
multi-machine collaboration — Bob's agent seeing Alice's in-flight instance,
dispatch routing notifications to a reviewer on another box — git is too slow
(you'd have to commit/push to share state). This needs a networked append-only
event log that instances sync to. **It must be a pluggable adapter, not engine
core.** The engine emits events (it already writes them to `log.txt` and
instance files); a sync adapter ships them to a shared store. Keep the engine
ignorant of the network: it writes events locally and fails open if the adapter
is down ([Fail Open](../principles/fail-open.md) — unreachable substrate ⇒
degrade to local, never block). This is the boundary the conops already drew:
"the engine receives signals; the delivery mechanism is pluggable."

This two-tier split lets a team get 80% of the value (shared learnings, shared
analytics, async handoff) from git today, and opt into the networked 20%
(real-time co-presence) only when they need it and can run infrastructure.

### 3.4 Semantic substrate: learnings as gated records

This is the genuinely new artifact and the one most likely to become a swamp.
The discipline:

**Learnings are records produced *by processes*, not free-floating notes.**
Turnstile already ships the producers: `decision-record`, `retrospective`,
`scientific-method` are exactly reflection workflows whose *output* is durable
knowledge. Today their output is an ad-hoc markdown file (`output_file`
parameter). Formalize that output into a structured, addressable **learning**:

```yaml
# .memory/learnings/2026-06-auth-token-race.yaml  (committed)
id: auth-token-race
kind: gotcha                       # gotcha | convention | decision | baseline
scope:                             # how retrieval is filtered (avoids context bloat)
  processes: [feature-development, bug-fix]
  paths: ["src/auth/**"]
  roles: [developer, reviewer]
summary: "Token refresh races under concurrent requests; serialize via the lock in session.py."
detail_ref: docs/notes/auth-token-race.md   # pointer, not inlined (render-don't-record)
produced_by: { process: retrospective, instance: 0b7737 }
freshness:
  review_by: 2026-12-01            # semantic memory perishes; force re-validation
created: 2026-06-03
```

Why this shape:
- **Scope tags are the anti-bloat mechanism.** The conops flags the core risk of
  a broadcast feed: it "costs context window space… could crowd out more useful
  context." Scoping by process/path/role means retrieval is *filtered*, never a
  firehose. A reviewer touching `src/auth/` sees the auth gotcha; nobody else
  does.
- **`detail_ref` is a pointer, not content** — straight from the
  [compaction-resilience](../spikes/compaction-resilience.md) playbook: keep the
  surfaced unit tiny ("read this file"), let the detail live in a file the agent
  can re-read. Compaction-safe by construction.
- **`freshness.review_by`** acknowledges that semantic memory rots. Reuse the
  `max_age`/evidence pattern: a "memory-gardening" process (or a CI gate) flags
  learnings past review-by for re-validation or retirement. **Stale semantic
  memory is worse than none** — it's the [Portable by Default](../principles/portable-by-default.md)
  "failures train behavior" hazard applied to knowledge: act on one wrong
  learning and the agent learns to distrust all of them.

### 3.5 Retrieval and surfacing — the hard half

Storing memory is easy; surfacing the *right* memory at the *right* moment
without drowning the context window is the whole game. [50 First
Dates](../principles/50-first-dates.md) says context is the only interface;
[Progressive Disclosure](../principles/progressive-disclosure.md) says show the
minimum and link to depth. Three surfacing points, all reusing existing
machinery:

1. **SessionStart hook** — on session start, surface active shared instances +
   learnings scoped to the project/role. This is the conops' "situational
   awareness for this shift." Pre-filtered by scope, capped in size.
2. **The guard nudge (PreToolUse)** — the
   [compaction-resilience spike](../spikes/compaction-resilience.md) already
   proposes extending the guard to re-inject `agent_context` on every
   Edit/Write. Extend it once more: when the edited path matches a learning's
   `scope.paths`, surface that learning's one-line `summary` + `detail_ref`. The
   agent editing `src/auth/token.py` is reminded of the token race *at the moment
   it matters*, in one line, re-injected every mutation (compaction can't outrun
   it).
3. **A `process_recall` / `memory_query` MCP tool** — explicit pull:
   "learnings for state X / role Y / path Z." Render-don't-record applied to
   retrieval — the agent queries current truth rather than carrying a stale dump.

`agent_context.reference_files` is the existing seam: a role spec can point at the
learnings relevant to that role, and they get surfaced through the channel that
already exists. No new injection mechanism required.

### 3.6 Write discipline — gate what enters memory

The failure mode of every team memory system is accretion: it fills with
duplicated, stale, low-signal entries until nobody trusts it. The fix is
on-brand: **make writing to semantic memory a gated process.** A
`capture-learning` definition with validation gates that enforce the discipline
mechanically:

- **Durability gate:** "Will this be wrong tomorrow without an update?" If yes,
  it's *state* (it belongs in a gate/query, not a learning) — reject. This is
  [Render Don't Record](../principles/render-dont-record.md) as an enforced
  check. A learning that says "there are 12 open auth bugs" is rejected; one that
  says "run `git grep TODO src/auth` to find them" is accepted.
- **Dedup gate:** `process_recall` for similar scope; if a near-duplicate exists,
  amend it rather than adding a second copy ([Progressive Disclosure](../principles/progressive-disclosure.md):
  a fact lives in exactly one place).
- **Scope gate:** must declare scope tags, or it's unretrievable noise.

Episodic memory needs no such gate — it's immutable record, written mechanically
by the engine. Only *semantic* memory, the curated layer, is gated. This keeps
the substrate a garden, not a landfill.

### 3.7 Collaboration mechanics, concurrency, and scope

- **Anti-siloing via shared instances:** with the episodic ledger shared (§3.3),
  the conops' four-session workflow works — any qualified operator picks up any
  instance because the instance (the "equipment") is on a shared job site, not a
  laptop. The dispatch/wait/handoff primitives the conops describes finally have
  the substrate they assumed.
- **Learning flow:** a developer hits an edge case → posts to the ephemeral
  broadcast feed (situational awareness, e.g. via agent mail) *and*, if durable,
  the next `retrospective`/`capture-learning` run distills it into a committed
  learning. Ephemeral feed for "heads up right now"; gated semantic store for
  "this should outlive the week." Two channels, two lifetimes — don't conflate.
- **Concurrency:** shared *mutable* state needs a guard (conops open item #5).
  File locking suffices on one machine; the synced store (Tier B) needs
  compare-and-swap / leases on the instance record. Sidestep most of it by
  keeping `active/` local and sharing only the immutable ledger — you only need
  real concurrency control when two machines mutate one live instance, which is
  the advanced case.
- **Scope & privacy:** project-local vs. org-wide learnings; `roles` as
  (advisory-first, enforce-later) access boundaries, mirroring the existing
  enforcement spectrum. Compliance audit trails are the explicit Layer-3 payoff —
  the ledger *is* the audit trail.

### 3.8 Fit with principles and the decision filter

- **Engine stays small (filter Q4 "definition or engine?"):** the engine gains
  only an **event-emit seam** + a pluggable **sync-adapter interface**. Learnings,
  reflection processes, recall tool, gardening — all definitions and tooling.
- **Render Don't Record is load-bearing, not decorative:** it's what makes
  episodic memory safe to share (records, not state) and what the durability gate
  enforces on semantic writes.
- **Fail Open:** unreachable substrate ⇒ local-only operation, surfaced as a
  degraded-mode warning, never a block.
- **Portable by Default:** the git-first tier works in any repo; the networked
  tier is opt-in for teams that can run it. No definition should *require* the
  shared store to function.
- **Trust, not capability (filter Q5):** a shared, auditable record of what every
  agent did and learned is precisely the "explain to your team lead what the
  agent did and why" that the [vision](../vision.md#thesis) names as the real
  blocker to shipping agents.

### 3.9 Phased roadmap

1. **Episodic ledger in git** — split `active/` (local) from a committed
   append-only `.process-ledger/`. Pure persistence change; immediately enables
   team-wide analytics and trace-mined evals (§2). Highest value / lowest risk.
2. **Structured learnings + `capture-learning` process + `process_recall`** —
   formalize reflection-process output; gated writes; scoped retrieval.
3. **Surfacing** — extend the guard nudge and SessionStart hook to inject
   path/role-scoped learnings (compaction-safe, pointer-based).
4. **Memory gardening** — freshness gates, dedup, retirement of stale learnings.
5. **Sync adapter (Layer-3, optional)** — networked event store for real-time
   multi-machine co-presence and dispatch routing. Pluggable, fail-open.
6. **Cross-project fabric** — org-wide learning packs distributed like process
   packs via `extends`; compliance audit views over the ledger.

---

## 4. How the two systems interlock

They are not independent; building them together is cheaper than building either
alone, and each makes the other trustworthy:

- **Episodic ledger → eval generation.** Tier-2 of System 2 (the shared,
  committed instance ledger) is the data source for §2.3(b) trace-mined evals.
  Build the ledger once; both features consume it.
- **Analytics baselines are shared memory *and* production evals.** §2.4(3) and
  §3.3 are the same artifact viewed from two angles: a versioned, team-shared
  metric baseline that doubles as a continuously-running regression assertion.
- **Failed evals → semantic learnings.** When a behavioral eval fails and gets
  diagnosed, the diagnosis is a learning ("the `release` process's staging gate
  flakes when the cluster has <3 nodes"). System 1 feeds System 2.
- **Learnings → better evals.** A captured gotcha is a hint for the
  `eval-authoring` synthesizer about which adversarial scenarios matter. System 2
  feeds System 1.
- **Both are dogfooded as processes.** `eval-authoring`, `capture-learning`,
  `memory-gardening` are new starter-pack definitions exercised against
  Turnstile's own development — exactly how the existing starter pack was built.

The deepest connection: **both close the loop on the [50 First
Dates](../principles/50-first-dates.md) problem at the team scale.** Today
Turnstile gives a fresh agent procedural memory (the definition). With these two
systems, a fresh agent on *any* team member's machine inherits the *proven
guarantees* (evals) and the *accumulated learnings and state* (substrate) of
everyone who came before it. The agent still starts from zero context — but "zero
context" now resolves against a shared, curated, verifiable team memory instead
of one developer's gitignored directory.

---

## 5. Risks and open questions

**Evals**
- **Behavioral non-determinism vs. CI.** Tier-2 evals can't gate CI reliably
  (flaky by nature). Treat them as *monitoring with trend lines*, not pass/fail
  gates; only Tier-1 blocks. Don't let a flaky judge erode trust the way a broken
  gate does ([Portable by Default](../principles/portable-by-default.md),
  "failures train behavior").
- **Generation quality.** Auto-generated evals can encode the *current* behavior
  as "correct," cementing bugs. Hence the human/peer-review gate in
  `eval-authoring` and the rule that traces are *promoted*, never auto-adopted.
- **Cost.** Behavioral evals burn tokens. Keep them opt-in, sampled, and
  scoped to the gates that matter.

**Shared memory**
- **Concurrency on shared mutable state** (conops #5) — genuinely hard; mitigated
  by sharing only the immutable ledger first.
- **The git-merge story for the ledger** — append-only, instance-per-file layout
  keeps conflicts rare, but month-directory archiving and the single `log.txt`
  need a conflict-resistant rethink (e.g., per-instance files only, no shared
  append target in git).
- **Semantic memory rot and over-trust** — the freshness gate and gardening
  process are essential, not optional; an unmaintained learning store is a
  liability.
- **Scope creep past "engine stays small."** The strongest discipline is the
  [decision filter](../vision.md#decision-filter): if it can be a definition or a
  pluggable adapter, it must not be engine code. Networking, in particular, lives
  behind an adapter or it poisons the engine's portability and testability.

**Cross-cutting**
- Both features assume an agent runtime that honors surfaced context (the
  [compaction-resilience](../spikes/compaction-resilience.md) contract). The
  engine can surface; it cannot force. This caps achievable behavioral
  consistency and should be stated honestly to users.

---

## 6. Recommended next steps

Smallest moves that unlock the most, in order:

1. **`dry_run` simulation mode** (inject mocked gate outcomes) — one focused
   engine change; unlocks all of Tier-1 evals.
2. **Promote `completed/` to a committed, append-only ledger** (split from local
   `active/`) — one persistence change; unlocks team analytics, trace-mined
   evals, and the entire episodic substrate.
3. **`turnstile eval` runner + graph-derived generator + coverage report** —
   deterministic, CI-able, immediately useful for process development.
4. **`capture-learning` + `process_recall` + guard-nudge surfacing** — the
   semantic substrate MVP, built as a definition + small tooling, gated for
   write discipline.

Items 1–2 are the keystones: each is a contained change, and between them they
provide the foundation both larger systems stand on. Everything above them is
definitions, tooling, and adapters — exactly where, per Turnstile's own vision,
the domain knowledge is supposed to live.

---

### Appendix: primitives this report would add to the engine

Deliberately minimal — everything else is definitions/tooling/adapters.

| Primitive | Why it must be engine-level | Used by |
|---|---|---|
| `dry_run` gate-world injection (simulation mode) | Only the runtime can mock gate execution while preserving transition logic | Tier-1 evals |
| `expect: blocked` assertion verb in eval runs | Needs to observe the engine's own reject path | Tier-1 evals |
| Event-emit seam (already ~present in `log.txt`/instance writes) | Sync adapters subscribe here | Shared substrate (Tier B) |
| Pluggable sync-adapter interface (fail-open) | Keeps networking out of the core | Shared substrate (Tier B) |
| Committed-ledger vs. local-active split in `StateStore` | Persistence layout is engine-owned | Episodic substrate |

Everything else — eval artifacts, generators, judges, learning records, recall,
gardening, reflection processes — is a **definition or tooling layer**, per
decision-filter Q4. The engine stays small; the intelligence lives in the
definitions.
