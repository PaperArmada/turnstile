"""Tests for engine-side trail integrity: automatic anchoring and
git-SHA stamping driven by registry verification settings."""

import json
import subprocess

import pytest
import yaml

from turnstile_core.definition.model import RegistrySettings
from turnstile_core.instance.trail import Anchor, compute_chain_head
from turnstile_core.ops.verify import latest_instance_id
from turnstile_core.runtime.engine import Engine

PROCESS = {
    "name": "simple",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["work"]},
        {"id": "work", "transitions": ["done"]},
        {"id": "done", "type": "terminal"},
    ],
}


def sh(cwd, *args):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def make_project(tmp_path, *, settings: dict | None = None, git: bool = False):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    proc_dir = root / ".processes"
    proc_dir.mkdir()
    (proc_dir / "simple.yaml").write_text(yaml.dump(PROCESS))
    if settings:
        (proc_dir / "registry.yaml").write_text(
            yaml.dump({"version": "1.0", "settings": settings})
        )
    if git:
        sh(root, "git", "init", "-q")
        sh(root, "git", "config", "user.email", "t@example.com")
        sh(root, "git", "config", "user.name", "T")
        (root / "f.txt").write_text("x\n")
        sh(root, "git", "add", "-A")
        sh(root, "git", "commit", "-qm", "initial")
    return root


class TestVerificationSettings:
    def test_defaults(self):
        s = RegistrySettings()
        assert s.verification.record_git_sha is True
        assert s.verification.anchor_file == ""
        assert s.verification.anchor_command == ""

    def test_parse_from_registry(self, tmp_path):
        root = make_project(tmp_path, settings={
            "verification": {
                "anchor_file": str(tmp_path / "outside" / "anchors.json"),
                "signal_key_file": "~/.turnstile/operator.key",
            }
        })
        engine = Engine(root)
        assert engine.registry.settings.verification.anchor_file


class TestAutomaticAnchoring:
    async def test_every_persistence_event_anchors(self, tmp_path):
        anchor_file = tmp_path / "outside" / "anchors.json"
        root = make_project(tmp_path, settings={
            "verification": {"anchor_file": str(anchor_file)}
        })
        engine = Engine(root)

        iid = engine.start("simple")["instance_id"]
        assert anchor_file.exists()
        first = json.loads(anchor_file.read_text())[iid]

        await engine.transition(iid, "work")
        second = json.loads(anchor_file.read_text())[iid]
        assert second != first
        assert second["entries"] == 1

        await engine.transition(iid, "done")
        final = json.loads(anchor_file.read_text())[iid]
        assert final["entries"] == 2
        instance = engine._store.load_any(iid)
        assert final["head"] == compute_chain_head(instance)

    async def test_no_anchor_without_config(self, tmp_path):
        root = make_project(tmp_path)
        engine = Engine(root)
        iid = engine.start("simple")["instance_id"]
        await engine.transition(iid, "work")
        assert engine._store.anchor is None

    def test_anchor_command_backend(self, tmp_path):
        out = tmp_path / "cmd-anchor.txt"
        anchor = Anchor(command=f"echo '{{instance_id}} {{head}} {{entries}}' >> {out}")
        root = make_project(tmp_path)
        engine = Engine(root)
        iid = engine.start("simple")["instance_id"]
        instance = engine._store.load_any(iid)
        anchor.record(instance)
        line = out.read_text().strip()
        assert line.startswith(iid)
        assert compute_chain_head(instance) in line

    def test_anchor_failure_is_fail_open(self, tmp_path):
        anchor = Anchor(command="exit 1")
        root = make_project(tmp_path)
        engine = Engine(root)
        iid = engine.start("simple")["instance_id"]
        # Must not raise
        anchor.record(engine._store.load_any(iid))


class TestShaStamping:
    async def test_transitions_record_head_sha(self, tmp_path):
        root = make_project(tmp_path, git=True)
        engine = Engine(root)
        head = sh(root, "git", "rev-parse", "HEAD")

        iid = engine.start("simple")["instance_id"]
        await engine.transition(iid, "work")
        instance = engine._store.load(iid)
        assert instance.history[-1].metadata["git_sha"] == head

    async def test_explicit_sha_not_overwritten(self, tmp_path):
        root = make_project(tmp_path, git=True)
        engine = Engine(root)
        iid = engine.start("simple")["instance_id"]
        await engine.transition(iid, "work", metadata={"git_sha": "explicit"})
        instance = engine._store.load(iid)
        assert instance.history[-1].metadata["git_sha"] == "explicit"

    async def test_no_git_repo_is_fail_open(self, tmp_path):
        root = make_project(tmp_path, git=False)
        engine = Engine(root)
        iid = engine.start("simple")["instance_id"]
        await engine.transition(iid, "work")
        instance = engine._store.load(iid)
        assert "git_sha" not in instance.history[-1].metadata

    async def test_disabled_by_settings(self, tmp_path):
        root = make_project(
            tmp_path, git=True,
            settings={"verification": {"record_git_sha": False}},
        )
        engine = Engine(root)
        iid = engine.start("simple")["instance_id"]
        await engine.transition(iid, "work")
        instance = engine._store.load(iid)
        assert "git_sha" not in instance.history[-1].metadata

    async def test_skip_records_sha(self, tmp_path):
        root = make_project(tmp_path, git=True)
        engine = Engine(root)
        iid = engine.start("simple")["instance_id"]
        await engine.skip(iid, "done", reason="test")
        instance = engine._store.load_any(iid)
        assert instance.history[-1].metadata.get("git_sha")


class TestLatestInstance:
    async def test_finds_most_recent_completed(self, tmp_path):
        root = make_project(tmp_path)
        engine = Engine(root)

        first = engine.start("simple")["instance_id"]
        await engine.transition(first, "work")
        await engine.transition(first, "done")

        second = engine.start("simple")["instance_id"]
        await engine.transition(second, "work")
        await engine.transition(second, "done")

        assert latest_instance_id(root, "simple") == second

    def test_none_when_no_completed(self, tmp_path):
        root = make_project(tmp_path)
        Engine(root)  # creates the state dir
        assert latest_instance_id(root, "simple") is None
