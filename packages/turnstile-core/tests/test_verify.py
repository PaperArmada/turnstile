"""Tests for turnstile_core.ops.verify — acceptance-time verification.

These are the CI-durable versions of the attack scenarios in
scripts/poc_guarantee.py: each mechanism from docs/guarantee.md has a
test proving it grants on conformance and refuses on attack.
"""

import json
import subprocess

import pytest

from turnstile_core.instance.model import HistoryEntry, ProcessInstance
from turnstile_core.ops.verify import (
    AcceptancePolicy,
    ReverifyGate,
    compute_chain_head,
    sign_signal,
    signal_signature_valid,
    verify_instance,
    write_anchor,
)
from turnstile_core.runtime.engine import Engine

PROCESS_YAML = """\
name: guarded-release
version: "1.0.0"
states:
  - id: start
    type: initial
    transitions: [build]
  - id: build
    transitions: [approval]
    on_exit:
      validate:
        - command: "cat artifact.txt"
          expect: contains("built-ok")
          message: "Release artifact must exist and be valid"
  - id: approval
    type: wait
    signal:
      name: release_approval
      required_fields:
        - {key: approved}
    transitions: [ship]
  - id: ship
    type: terminal
"""

KEY = b"operator-secret"


def sh(cwd, *args):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def sandbox(tmp_path):
    """Project dir (agent's world) + outside dir (operator's world)."""
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / ".processes").mkdir()
    (root / ".processes" / "guarded-release.yaml").write_text(PROCESS_YAML)
    sh(root, "git", "init", "-q")
    sh(root, "git", "config", "user.email", "agent@example.com")
    sh(root, "git", "config", "user.name", "Agent")
    (root / "README.md").write_text("demo\n")
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-qm", "initial")
    (outside / "operator.key").write_bytes(KEY)
    return {
        "root": root,
        "outside": outside,
        "base": sh(root, "git", "rev-parse", "HEAD"),
        "engine": Engine(root),
    }


def make_policy(sandbox) -> AcceptancePolicy:
    return AcceptancePolicy(
        process="guarded-release",
        required_states=["build", "approval", "ship"],
        allow_skips=False,
        reverify=[ReverifyGate(state="build", hook="on_exit")],
        human_signal_states=["approval"],
        signal_key_file=str(sandbox["outside"] / "operator.key"),
        anchor_file=str(sandbox["outside"] / "anchors.json"),
        enforce_binding=True,
    )


async def run_workflow(sandbox, *, signed=True) -> str:
    e = sandbox["engine"]
    root = sandbox["root"]
    anchor_file = sandbox["outside"] / "anchors.json"

    def anchor(iid):
        write_anchor(anchor_file, e._store.load_any(iid))

    iid = e.start("guarded-release")["instance_id"]
    anchor(iid)
    await e.transition(iid, "build")
    anchor(iid)

    (root / "artifact.txt").write_text("built-ok\n")
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-qm", "build artifact")
    head = sh(root, "git", "rev-parse", "HEAD")

    r = await e.transition(iid, "approval", metadata={"git_sha": head})
    assert r.success
    anchor(iid)

    data = {"approved": True, "approved_by": "operator@example.com"}
    if signed:
        data["signature"] = sign_signal(KEY, iid, "release_approval", data)
    r = await e.receive_signal(iid, "release_approval", data, target_state="ship")
    assert r["new_state"] == "ship"
    anchor(iid)
    return iid


# ---------------------------------------------------------------------------
# Unit: hash chain and signatures
# ---------------------------------------------------------------------------


def make_instance(**overrides) -> ProcessInstance:
    data = {
        "instance_id": "abc123",
        "process_name": "p",
        "current_state": "done",
        "started_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T01:00:00+00:00",
        "history": [
            {"from": "start", "to": "work", "at": "t1"},
            {"from": "work", "to": "done", "at": "t2"},
        ],
    }
    data.update(overrides)
    return ProcessInstance(**data)


class TestChain:
    def test_deterministic(self):
        assert compute_chain_head(make_instance()) == compute_chain_head(make_instance())

    def test_any_edit_changes_head(self):
        original = compute_chain_head(make_instance())
        edited = make_instance()
        edited.history[0] = HistoryEntry(**{"from": "start", "to": "done", "at": "t1"})
        assert compute_chain_head(edited) != original

    def test_truncation_changes_head(self):
        original = compute_chain_head(make_instance())
        truncated = make_instance()
        truncated.history.pop()
        assert compute_chain_head(truncated) != original


class TestSignatures:
    def test_roundtrip(self):
        data = {"approved": True, "approved_by": "op"}
        data["signature"] = sign_signal(KEY, "abc123", "go", data)
        assert signal_signature_valid(KEY, "abc123", "go", data)

    def test_wrong_key_rejected(self):
        data = {"approved": True}
        data["signature"] = sign_signal(b"other-key", "abc123", "go", data)
        assert not signal_signature_valid(KEY, "abc123", "go", data)

    def test_payload_mutation_rejected(self):
        data = {"approved": True}
        data["signature"] = sign_signal(KEY, "abc123", "go", data)
        data["approved"] = False
        assert not signal_signature_valid(KEY, "abc123", "go", data)

    def test_missing_signature_rejected(self):
        assert not signal_signature_valid(KEY, "abc123", "go", {"approved": True})


# ---------------------------------------------------------------------------
# Integration: the attack scenarios
# ---------------------------------------------------------------------------


class TestVerifyScenarios:
    async def test_clean_run_passes(self, sandbox):
        iid = await run_workflow(sandbox)
        report = await verify_instance(
            sandbox["root"], iid, make_policy(sandbox),
            commit_range=f"{sandbox['base']}..HEAD",
        )
        assert report.passed, report.render()
        statuses = {c.status for c in report.checks}
        assert {"PROVEN", "ATTESTED", "HUMAN"} <= statuses

    async def test_tampered_ledger_fails(self, sandbox):
        iid = await run_workflow(sandbox)
        store = sandbox["engine"]._store
        path = next(store.completed_dir.rglob(f"*{iid}.json"))
        data = json.loads(path.read_text())
        data["history"][1]["to"] = "ship"
        path.write_text(json.dumps(data))

        report = await verify_instance(
            sandbox["root"], iid, make_policy(sandbox),
            commit_range=f"{sandbox['base']}..HEAD",
        )
        assert not report.passed
        assert any(
            c.status == "FAIL" and c.name == "trail integrity"
            for c in report.checks
        )

    async def test_gate_reexecution_catches_reality_drift(self, sandbox):
        iid = await run_workflow(sandbox)
        (sandbox["root"] / "artifact.txt").write_text("corrupted\n")

        report = await verify_instance(
            sandbox["root"], iid, make_policy(sandbox),
            commit_range=f"{sandbox['base']}..HEAD",
        )
        assert not report.passed
        assert any(
            c.status == "FAIL" and c.name.startswith("gate re-execution")
            for c in report.checks
        )

    async def test_self_approval_fails(self, sandbox):
        iid = await run_workflow(sandbox, signed=False)
        report = await verify_instance(
            sandbox["root"], iid, make_policy(sandbox),
            commit_range=f"{sandbox['base']}..HEAD",
        )
        assert not report.passed
        assert any(
            c.status == "FAIL" and c.name.startswith("human signal")
            for c in report.checks
        )

    async def test_uncertified_commit_fails_binding(self, sandbox):
        iid = await run_workflow(sandbox)
        (sandbox["root"] / "sneaky.py").write_text("# uncertified\n")
        sh(sandbox["root"], "git", "add", "-A")
        sh(sandbox["root"], "git", "commit", "-qm", "extra work")

        report = await verify_instance(
            sandbox["root"], iid, make_policy(sandbox),
            commit_range=f"{sandbox['base']}..HEAD",
        )
        assert not report.passed
        assert any(
            c.status == "FAIL" and c.name == "artifact binding"
            for c in report.checks
        )

    async def test_skip_fails_exception_policy(self, sandbox):
        e = sandbox["engine"]
        iid = e.start("guarded-release")["instance_id"]
        await e.transition(iid, "build")
        (sandbox["root"] / "artifact.txt").write_text("built-ok\n")
        await e.skip(iid, "ship", reason="in a hurry")
        write_anchor(
            sandbox["outside"] / "anchors.json", e._store.load_any(iid)
        )

        report = await verify_instance(
            sandbox["root"], iid, make_policy(sandbox),
            commit_range=f"{sandbox['base']}..HEAD",
        )
        assert not report.passed
        assert any(
            c.status == "FAIL" and c.name == "exceptions"
            for c in report.checks
        )

    async def test_allowed_skips_pass_exception_policy(self, sandbox):
        e = sandbox["engine"]
        iid = e.start("guarded-release")["instance_id"]
        await e.transition(iid, "build")
        (sandbox["root"] / "artifact.txt").write_text("built-ok\n")
        await e.skip(iid, "ship", reason="hotfix, approved out of band")
        write_anchor(
            sandbox["outside"] / "anchors.json", e._store.load_any(iid)
        )

        policy = make_policy(sandbox)
        policy.allow_skips = True
        policy.required_states = ["build", "ship"]
        policy.enforce_binding = False

        report = await verify_instance(sandbox["root"], iid, policy)
        assert report.passed, report.render()

    async def test_incomplete_instance_fails(self, sandbox):
        e = sandbox["engine"]
        iid = e.start("guarded-release")["instance_id"]
        await e.transition(iid, "build")
        write_anchor(
            sandbox["outside"] / "anchors.json", e._store.load_any(iid)
        )
        policy = make_policy(sandbox)
        policy.enforce_binding = False
        policy.reverify = []

        report = await verify_instance(sandbox["root"], iid, policy)
        assert not report.passed
        assert any(
            c.status == "FAIL" and c.name == "completion" for c in report.checks
        )
