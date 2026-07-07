# The Guarantee

> Can Turnstile genuinely guarantee that a workflow was followed?
> Yes — and this document is the proof sketch: the adversary, the
> attack surface, what is proven versus attested, and what we refuse
> to claim. If a future feature weakens any statement here, that
> feature is wrong, not this document.

## The adversary

A sloppy or reward-hacking agent with **full read/write access to its
own environment**: the working tree, `.process-state/`, local git,
every file the engine touches, and every MCP tool the engine exposes.

Out of scope: a malicious human with repository admin rights. No
tooling defends against the owner of the infrastructure; we defend
against the *agent*.

## The foundational limit

**Anything computed inside the agent's environment can be fabricated
by the agent.**

A hash chain alone proves nothing to an adversary who can rewrite the
whole chain. A signing key stored in the environment is a key the
agent can read. It follows that a genuine guarantee can only rest on
things outside the agent's reach:

1. **Recomputation** — verification re-executed in an environment the
   agent doesn't control (CI on the merge commit).
2. **External anchoring** — evidence deposited outside the environment
   *while the work happens*, under credentials the agent doesn't hold.
3. **Authenticated identity** — human judgments delivered through a
   channel that proves who signed, not a tool the agent can call.
4. **Server-side acceptance control** — branch protection and required
   checks, configured where the agent has no write access.

Everything below is an application of these four escapes.

## Attack surface, attack by attack

### 1. Forge the ledger

*"Write a plausible instance file showing every state visited and
every gate passed."*

**Closed by not trusting the ledger for anything that matters.**
`turnstile verify` re-executes gates in a clean environment against
the exact commit being merged. Recorded gate results are a performance
log; the proof is recomputed. This is what render-don't-record buys:
because gates compute reality rather than record claims, the audit
trail is *reproducible* — the forgery question evaporates for every
re-runnable gate.

For claims that cannot be re-run (the walk order, the timing), the
trail is hash-chained and each entry's hash is **anchored outside the
environment contemporaneously** (e.g., pushed as a git note or posted
to an attestation endpoint under a credential the agent does not
hold). Backfilling a forged history then requires compromising
infrastructure outside the environment — infeasible, not merely
detectable.

### 2. Hollow states

*"Transition through `review` in four seconds without reviewing."*

The engine proves **structure**, never **judgment** — see
[vision.md](vision.md) on structural vs. behavioral consistency. The
mitigations are the ones the definition format already provides:

- Gates on the **outputs** of the state ("does the review document
  exist and reference the changed files?").
- **Wait states with human signals** for judgment-heavy steps.

Substance is provable exactly to the extent it is expressed as a
checkable predicate or a human sign-off. Process authors control that
dial; the engine makes whatever they set mechanically binding.

### 3. Gamed gates

*"Satisfy the letter of the check, not its intent."*

Turnstile faithfully enforces the predicate you wrote; it cannot
enforce the intention behind it. This is not a weakness unique to
Turnstile — it is the exact deal offered by types, tests, and CI, and
it is the deal all of software correctness runs on. **The guarantee is
as strong as the specification.** Our obligation is to make strong
specifications easy: first-class, portable gate patterns instead of
grep-and-hope shell strings.

### 4. Bypass the choke point

*"Push to main directly. Run `git commit --no-verify`. Disable the
workflow."*

Local git hooks are theater against this adversary; they are
convenience, not enforcement. The real turnstile is **server-side**:
protected branch, `turnstile verify` as a required status check, no
direct pushes, no admin bypass for the agent's credentials. That
configuration lives where the agent cannot write.

It is, however, configuration the *operator* must set — so the CLI
must ship **`turnstile doctor`**: a command that inspects the
acceptance boundary (branch protection on? check required? direct
pushes off?) and reports exactly which screw is loose. A guarantee
with a silent misconfiguration is worse than no guarantee.

### 5. The binding hole

*"Walk the workflow dutifully on trivial work, then land different
work."*

An instance that is not bound to specific commits certifies only that
*a process ran*, not that *this diff followed it*. Therefore:

- Every history entry records the **git SHA** at transition time.
- `verify` checks that the commit range being merged is **covered** by
  the certifying instance(s): commits fall within the instance's
  lifetime and match its recorded checkpoints.

The trail certifies the artifact, or it certifies nothing.

### 6. The self-approval hole

*"The wait state expects a human signal — deliver it yourself."*

`process_signal` is an engine tool; an agent can currently call it. A
"human approval" the agent can spoof is worse than none, because it
manufactures false confidence. Therefore human signals must arrive
through **authenticated identity channels** the agent does not hold:
a GitHub PR approval, a signed CLI invocation under the operator's
credentials, a chat-ops action. The engine records *who* signed as
verified identity, and `verify` treats an unauthenticated signal on a
human-gated wait state as a conformance failure.

## The theorem

With the above in place, for exactly the code being merged,
`turnstile verify` establishes:

**Proven** (recomputed; does not rely on the ledger at all):
- Every re-runnable gate passes in a clean environment.
- The merged commit range is bound to the certifying instance.
- The exception list is complete, and every skip/override was
  authorized by the acceptance policy.

**Attested** (tamper-evident and contemporaneously anchored;
falsifying it requires compromising infrastructure outside the
agent's environment):
- The state walk happened, in the recorded order, at the recorded
  times.

**Human-verified**:
- Every declared judgment point carries an authenticated approval
  with a name attached.

**Never claimed**:
- The quality of reasoning inside a state, beyond what gates and
  humans checked. We say this out loud, everywhere, always.

The verify report renders this taxonomy explicitly, per item —

```
PROVEN    6 gates re-executed on a3f9c2e
ATTESTED  9 transitions, anchored, chain intact
HUMAN     1 approval (review_complete) — @operator
EXCEPT    0 skips, 0 overrides
```

— so the guarantee is legible and never oversold.

## The trusted computing base

What the operator must trust, in full:

1. Branch protection configuration (checkable by `turnstile doctor`).
2. The CI runner that executes `verify`.
3. The pinned `turnstile verify` binary/version.
4. The identity provider behind human signals.

All four live outside the agent's environment. **Nothing inside the
agent's environment needs to be trusted.** That is the formal content
of the claim "know without a doubt that my workflow was followed."

## What this demands of the engine

The engineering obligations this proof sketch creates (tracked in
[strategy.md](strategy.md), Phase 1):

1. `turnstile verify` with per-process acceptance policy.
2. Gate re-execution at verify time; gates classified re-runnable vs.
   advisory, reported as PROVEN vs. ATTESTED.
3. Git-SHA binding of history entries and commit-range coverage
   checking (closes the binding hole).
4. Authenticated human signals; unauthenticated signals on human-gated
   waits fail verification (closes the self-approval hole).
5. Hash-chained trail with contemporaneous external anchoring.
6. `turnstile doctor` for acceptance-boundary configuration.

## Implementation status

Every mechanism above is implemented (experimental surface — see
[STABILITY.md](../STABILITY.md); usage in [verification.md](verification.md)):

- **Engine-side automation** — with `settings.verification` configured
  in the registry, the engine stamps the git HEAD onto every history
  entry and deposits the chain head at the anchor on every persistence
  event. No agent cooperation required or trusted.
- **`turnstile verify [instance] --policy <yaml> [--range a..b]`** —
  the acceptance check, exiting 0/1; instance defaults to the latest
  completed run of the policy's process. Shipped as a GitHub Action
  (`action.yml`).
- **`turnstile approve`** — the operator identity channel: delivers a
  signal signed with a key held outside the agent's write domain.
- **`turnstile doctor`** — inspects the acceptance boundary (anchor
  and key placement, SHA recording, ledger visibility, branch
  protection) and fails on configuration that would void the
  guarantee.
- **`scripts/poc_guarantee.py`** — the attack demo: six scenarios
  (clean run, tampered ledger, reality drift, self-approval,
  uncertified commit, skipped review) staged against a real engine,
  each caught by exactly the mechanism this document predicts.
  Run it: `uv run python scripts/poc_guarantee.py`
- **`tests/test_verify.py`, `tests/test_trail.py`** — the same
  scenarios plus the engine-side automation as CI-durable tests.

## Honest limits, restated

Turnstile makes your workflow a **specification** and then makes that
specification binding at the point of acceptance. It cannot make a
weak specification strong, it cannot audit thought, and it cannot
protect a repository whose acceptance boundary is left open. Within
those stated limits, the guarantee is mechanical: **unverified work
cannot land.**
