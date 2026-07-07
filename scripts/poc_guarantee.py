"""Proof of concept for docs/guarantee.md — the attack demo.

Stages the highest-risk mechanisms of acceptance-time verification
against a real engine, one attack per scenario:

  1. clean run            -> PASS   (baseline: the guarantee grants)
  2. tampered ledger      -> FAIL   (hash chain vs external anchor)
  3. reality drift        -> FAIL   (gate re-execution beats claims)
  4. self-approval        -> FAIL   (unauthenticated human signal)
  5. uncertified commit   -> FAIL   (artifact binding)
  6. skipped review       -> FAIL   (exception policy)

Run:  uv run python scripts/poc_guarantee.py

Everything runs in throwaway temp directories. The "outside world"
(operator key, anchor file) lives in a separate directory standing in
for infrastructure the agent cannot write to; see the caveats in
turnstile_core/ops/verify.py.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from turnstile_core.ops.verify import (
    AcceptancePolicy,
    ReverifyGate,
    sign_signal,
    verify_instance,
    write_anchor,
)
from turnstile_core.runtime.engine import Engine

PROCESS_YAML = """\
name: guarded-release
description: "Release workflow with a build gate and human approval"
version: "1.0.0"

states:
  - id: start
    type: initial
    transitions: [build]

  - id: build
    description: "Produce the release artifact"
    transitions: [approval]
    on_exit:
      validate:
        - command: "cat artifact.txt"
          expect: contains("built-ok")
          message: "Release artifact must exist and be valid"

  - id: approval
    type: wait
    role: approver
    signal:
      name: release_approval
      required_fields:
        - {key: approved}
    transitions: [ship]

  - id: ship
    type: terminal
"""


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


class Sandbox:
    """A throwaway project (agent's world) plus an outside world the
    agent cannot write to (operator key, anchor file)."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tsg-project-"))
        self.outside = Path(tempfile.mkdtemp(prefix="tsg-outside-"))
        (self.root / ".processes").mkdir()
        (self.root / ".processes" / "guarded-release.yaml").write_text(PROCESS_YAML)
        sh(self.root, "git", "init", "-q")
        sh(self.root, "git", "config", "user.email", "agent@example.com")
        sh(self.root, "git", "config", "user.name", "Agent")
        (self.root / "README.md").write_text("demo\n")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-qm", "initial")
        self.base_sha = sh(self.root, "git", "rev-parse", "HEAD")
        self.key_file = self.outside / "operator.key"
        self.key_file.write_bytes(b"operator-secret-key-outside-agent-env")
        self.anchor_file = self.outside / "anchors.json"
        self.engine = Engine(self.root)

    def anchor(self, iid: str) -> None:
        """Simulate engine-side contemporaneous anchoring: after each
        transition, the chain head is deposited outside."""
        instance = self.engine._store.load_any(iid)
        write_anchor(self.anchor_file, instance)

    def policy(self) -> AcceptancePolicy:
        return AcceptancePolicy(
            process="guarded-release",
            required_states=["build", "approval", "ship"],
            require_completed=True,
            allow_skips=False,
            reverify=[ReverifyGate(state="build", hook="on_exit")],
            human_signal_states=["approval"],
            signal_key_file=str(self.key_file),
            anchor_file=str(self.anchor_file),
            enforce_binding=True,
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)


async def run_workflow(box: Sandbox, *, signed: bool = True, skip_approval: bool = False) -> str:
    """Drive the workflow like an agent would. Returns instance_id."""
    e = box.engine
    iid = e.start("guarded-release")["instance_id"]
    box.anchor(iid)

    r = await e.transition(iid, "build", session_id="agent")
    assert r.success
    box.anchor(iid)

    # Do the actual work: produce and commit the artifact
    (box.root / "artifact.txt").write_text("built-ok\n")
    sh(box.root, "git", "add", "-A")
    sh(box.root, "git", "commit", "-qm", "build artifact")
    head = sh(box.root, "git", "rev-parse", "HEAD")

    if skip_approval:
        r = await e.skip(iid, "ship", reason="in a hurry", session_id="agent")
        assert r.success
        box.anchor(iid)
        return iid

    r = await e.transition(
        iid, "approval", session_id="agent", metadata={"git_sha": head}
    )
    assert r.success, r.message
    box.anchor(iid)

    data: dict = {"approved": True, "approved_by": "operator@example.com"}
    if signed:
        data["signature"] = sign_signal(
            box.key_file.read_bytes(), iid, "release_approval", data
        )
    r = await e.receive_signal(
        iid, "release_approval", data, target_state="ship", session_id="agent"
    )
    assert r["new_state"] == "ship"
    box.anchor(iid)
    return iid


async def scenario(name: str, expect_pass: bool, prepare) -> bool:
    box = Sandbox()
    try:
        iid, commit_range = await prepare(box)
        report = await verify_instance(
            box.root, iid, box.policy(), commit_range=commit_range
        )
        ok = report.passed == expect_pass
        verdict = "as expected" if ok else "UNEXPECTED"
        print(f"\n=== {name} — expect {'PASS' if expect_pass else 'FAIL'} ({verdict}) ===")
        print(report.render())
        return ok
    finally:
        box.cleanup()


async def main() -> int:
    results = []

    # 1. Clean run: the workflow followed to the letter.
    async def clean(box: Sandbox):
        iid = await run_workflow(box)
        return iid, f"{box.base_sha}..HEAD"
    results.append(await scenario("1. clean run", True, clean))

    # 2. Tampered ledger: after completion, the agent rewrites history
    #    (e.g., disguising which states were walked). Chain vs anchor.
    async def tampered(box: Sandbox):
        iid = await run_workflow(box)
        store = box.engine._store
        path = next(store.completed_dir.rglob(f"*{iid}.json"))
        data = json.loads(path.read_text())
        data["history"][1]["to"] = "ship"  # forge: pretend build went straight to ship
        path.write_text(json.dumps(data))
        return iid, f"{box.base_sha}..HEAD"
    results.append(await scenario("2. tampered ledger", False, tampered))

    # 3. Reality drift: the ledger truthfully says the build gate passed
    #    during the run — but at acceptance time the claim no longer
    #    holds. Re-execution ignores the recorded result.
    async def drift(box: Sandbox):
        iid = await run_workflow(box)
        (box.root / "artifact.txt").write_text("corrupted\n")
        return iid, f"{box.base_sha}..HEAD"
    results.append(await scenario("3. reality drift (forged claim defeated by re-execution)", False, drift))

    # 4. Self-approval: the agent delivers the "human" signal itself,
    #    without the operator's signature.
    async def self_approval(box: Sandbox):
        iid = await run_workflow(box, signed=False)
        return iid, f"{box.base_sha}..HEAD"
    results.append(await scenario("4. self-approved wait state", False, self_approval))

    # 5. Uncertified commit: work landed after the certified snapshot.
    #    The trail must certify THIS diff, not just that a process ran.
    async def uncertified(box: Sandbox):
        iid = await run_workflow(box)
        (box.root / "sneaky.py").write_text("# slipped in after certification\n")
        sh(box.root, "git", "add", "-A")
        sh(box.root, "git", "commit", "-qm", "extra work outside the process")
        return iid, f"{box.base_sha}..HEAD"
    results.append(await scenario("5. uncertified commit", False, uncertified))

    # 6. Skipped review: the agent skips the approval state entirely.
    async def skipped(box: Sandbox):
        iid = await run_workflow(box, skip_approval=True)
        return iid, f"{box.base_sha}..HEAD"
    results.append(await scenario("6. skipped approval (override)", False, skipped))

    print("\n" + "=" * 60)
    if all(results):
        print(f"ALL {len(results)} SCENARIOS BEHAVED AS THE GUARANTEE PREDICTS")
        return 0
    print("SOME SCENARIOS DID NOT MATCH EXPECTATIONS")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
