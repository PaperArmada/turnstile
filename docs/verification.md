# Acceptance verification (experimental)

How to wire the guarantee from [guarantee.md](guarantee.md) into a real
project. Status: experimental — see [STABILITY.md](../STABILITY.md).

## 1. Configure the engine

In `.processes/registry.yaml`:

```yaml
settings:
  enforcement: monitor
  verification:
    record_git_sha: true                       # bind trail entries to commits
    anchor_file: ~/.turnstile/anchors.json     # MUST be outside the repo
    # anchor_command: "curl -fsS https://anchor.example/log -d '{instance_id}:{head}:{entries}'"
    signal_key_file: ~/.turnstile/operator.key # MUST be outside the repo
```

With this in place the engine automatically:

- stamps the current git SHA onto every history entry (transitions,
  signals, skips), and
- deposits the hash-chain head at the anchor on every persistence
  event — contemporaneous anchoring, no agent cooperation required.

Create the operator key (from *your* shell, not the agent's):

```bash
mkdir -p ~/.turnstile && head -c 32 /dev/urandom > ~/.turnstile/operator.key
```

## 2. Check the boundary

```bash
turnstile doctor
```

Doctor verifies the local half of the trusted computing base: anchor
and key placement (inside the repo = FAIL — the agent can reach them),
SHA recording, ledger visibility to CI, and branch protection where it
can. Fix FAIL items before trusting any green check.

Note on ledger visibility: `turnstile init` gitignores only
`.process-state/active/` and `.process-state/log.txt` — in-flight
state is ephemeral, but the **completed/abandoned ledger is the audit
record and is committed**, so the certification travels with the diff
and CI can read it. (Repos initialized before this convention should
update their `.gitignore`; `turnstile doctor` flags it.)

### Remote anchoring

A file anchor is only as good as its placement. For real deployments,
run the reference anchor server somewhere the agent has no
credentials:

```bash
python -m turnstile_core.ops.anchor_server --port 8123 --state /var/lib/turnstile
```

It enforces append-only semantics: an anchor may grow (more entries),
never shrink or change at the same entry count — rewrites get a 409
and land in the audit log. Point the engine at it:

```yaml
settings:
  verification:
    anchor_command: >-
      curl -fsS -X POST http://anchor.internal:8123/anchors
      -H 'Content-Type: application/json'
      -d '{"instance_id":"{instance_id}","head":"{head}","entries":{entries}}'
```

And in CI, fetch the snapshot before verifying:

```bash
curl -fsS http://anchor.internal:8123/anchors -o /tmp/anchors.json
turnstile verify --policy policy.yaml   # policy anchor_file: /tmp/anchors.json
```

## 3. Approve human gates as yourself

When an instance is waiting on a human-gated wait state, approve it
from your own shell with your key:

```bash
turnstile approve <instance-id> release_approval -d approved=true --to ship
```

The signature covers the instance, the signal name, and the payload.
An agent delivering the same signal without it will fail verification
— that is the point.

## 4. Write an acceptance policy

`.processes/policies/release.yaml`:

```yaml
process: guarded-release
required_states: [build, approval, ship]
require_completed: true
allow_skips: false
reverify:
  - {state: build, hook: on_exit}   # re-executed in CI; ledger not trusted
human_signal_states: [approval]     # must carry an operator signature
enforce_binding: true               # PR commits must be certified
```

`anchor_file` and `signal_key_file` are inherited from registry
settings when omitted here.

## 5. Verify locally, then require it in CI

```bash
turnstile verify --policy .processes/policies/release.yaml --range main..HEAD
```

(Instance ID optional — defaults to the latest completed instance of
the policy's process.)

In GitHub Actions:

```yaml
jobs:
  turnstile:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: PaperArmada/turnstile@main
        with:
          policy: .processes/policies/release.yaml
          require-pr-approval: 'true'
```

`require-pr-approval` is the GitHub-native human gate: the job fails
unless the PR carries an APPROVED review from someone other than its
author. Reviewer identity comes from GitHub authentication — an agent
cannot manufacture it. Use it for PR flows; use `turnstile approve`
(HMAC) for flows that never touch a PR.

`comment: 'true'` posts the full conformance report as a sticky PR
comment (updated in place on every run), so reviewers see the trail —
verification table, timeline, approvals, walk diagram — where they
already look. Give the job `pull-requests: write` permission.

## 6. The conformance report

```bash
turnstile report --policy .processes/policies/release.yaml --range main..HEAD -o report.md
```

`turnstile report` runs the same verification as `verify` (same exit
code — it can BE the CI check) and renders a self-contained Markdown
document: the verification table with PROVEN/ATTESTED/HUMAN statuses,
the transition timeline with gate counts and commit SHAs, exceptions
with reasons, approvals with signer identity, and a Mermaid diagram of
the walk. Use it for PR comments, CI summaries, or compliance
archives.

Then make the check required: repository settings → branch protection
on the default branch → require the `turnstile` job, disallow direct
pushes. That is the turnstile. `turnstile doctor` will confirm.

## What the statuses mean

- **PROVEN** — recomputed during verification; does not trust the
  stored ledger (re-executed gates, artifact binding, exception
  policy).
- **ATTESTED** — relies on the hash-chained trail matching its
  external anchor (state walk, ordering, timing).
- **HUMAN** — an authenticated operator approval.
- **INFO** — reported but not load-bearing (e.g. unanchored chains).
- **FAIL** — the workflow was not demonstrably followed; the check
  exits 1.
