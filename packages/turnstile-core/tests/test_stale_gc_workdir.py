"""Tests for stale-instance handling (guard collapse + gc), per-instance
work directories for gates/actions, and agent designation hints.

Added together because they shipped together: the guard-noise fix
(stale collapse), its cleanup companion (gc), the worktree fix
(instance-recorded project_dir), and the agent_context extension.
"""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from turnstile_core.admin import gc_stale_instances
from turnstile_core.engine import Engine
from turnstile_core.enforcement import check_enforcement

FIXTURES = Path(__file__).parent / "fixtures"

RESTRICTED_YAML = """
name: restricted
description: "Process with a no-edit state"
version: "1.0.0"
states:
  - id: start
    type: initial
    transitions: [frozen]
  - id: frozen
    description: "No edits allowed here"
    permissions:
      edit: false
    transitions: [done]
  - id: done
    type: terminal
"""


def _setup_project(tmp_path, enforcement="monitor", settings=None):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir(exist_ok=True)
    shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
    all_settings = {"enforcement": enforcement}
    all_settings.update(settings or {})
    (proc_dir / "registry.yaml").write_text(
        yaml.dump({"version": "1.0", "settings": all_settings})
    )
    return Engine(tmp_path)


def _backdate(tmp_path, instance_id, days):
    """Rewrite an active instance's updated_at to `days` ago on disk."""
    stamp = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()
    active_dir = tmp_path / ".process-state" / "active"
    for path in active_dir.glob("*.json"):
        data = json.loads(path.read_text())
        if data["instance_id"] == instance_id:
            data["updated_at"] = stamp
            path.write_text(json.dumps(data))
            return
    raise AssertionError(f"instance {instance_id} not found")


class TestStaleCollapse:
    def test_stale_instance_collapsed_in_guidance(self, tmp_path):
        engine = _setup_project(tmp_path)
        fresh = engine.start("simple", {"task_name": "fresh"})
        old = engine.start("simple", {"task_name": "old"})
        _backdate(tmp_path, old["instance_id"], days=170)

        result = check_enforcement(tmp_path)
        assert f"You are in: simple @ start (instance {fresh['instance_id']})" in result.guidance
        assert f"instance {old['instance_id']}" not in result.guidance
        assert "Stale (idle >= 14d, details suppressed)" in result.guidance
        assert f"simple @ start ({old['instance_id']}, idle 170d)" in result.guidance
        assert "turnstile gc" in result.guidance

    def test_fresh_instances_unaffected(self, tmp_path):
        engine = _setup_project(tmp_path)
        inst = engine.start("simple", {"task_name": "t"})
        result = check_enforcement(tmp_path)
        assert f"instance {inst['instance_id']}" in result.guidance
        assert "Stale (" not in result.guidance

    def test_threshold_configurable(self, tmp_path):
        engine = _setup_project(tmp_path, settings={"stale_after_days": 3})
        fresh = engine.start("simple", {"task_name": "fresh"})
        old = engine.start("simple", {"task_name": "old"})
        _backdate(tmp_path, old["instance_id"], days=5)
        result = check_enforcement(tmp_path)
        assert "Stale (idle >= 3d" in result.guidance
        assert f"instance {fresh['instance_id']}" in result.guidance
        assert f"instance {old['instance_id']}" not in result.guidance

    def test_zero_disables_collapse(self, tmp_path):
        engine = _setup_project(tmp_path, settings={"stale_after_days": 0})
        inst = engine.start("simple", {"task_name": "t"})
        _backdate(tmp_path, inst["instance_id"], days=400)
        result = check_enforcement(tmp_path)
        assert f"instance {inst['instance_id']}" in result.guidance
        assert "Stale (" not in result.guidance

    def test_stale_instance_still_enforces_permissions(self, tmp_path):
        """Collapse changes rendering only: a stale instance in a no-edit
        state still denies, and the deny names the restriction (not one of
        the unknown-state denies) plus the staleness remedy."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        (proc_dir / "restricted.yaml").write_text(RESTRICTED_YAML)
        (proc_dir / "registry.yaml").write_text(
            yaml.dump({"version": "1.0", "settings": {"enforcement": "enforce"}})
        )
        engine = Engine(tmp_path)
        inst = engine.start("restricted")
        # Move to the no-edit state, then backdate past the threshold
        active_dir = tmp_path / ".process-state" / "active"
        for path in active_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data["instance_id"] == inst["instance_id"]:
                data["current_state"] = "frozen"
                data["updated_at"] = (
                    datetime.now(timezone.utc) - timedelta(days=100)
                ).isoformat()
                path.write_text(json.dumps(data))
        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "deny"
        assert "does not allow edit" in result.reason
        # A stale blocker gets the cleanup remedy, not "transition first"
        assert "stale (idle 100d)" in result.reason
        assert "turnstile gc" in result.reason
        assert "Transition to a state" not in result.reason

    def test_all_stale_promotes_least_idle(self, tmp_path):
        """When every instance is stale, the least-idle one still renders
        in full so guidance keeps its post-compaction anchor."""
        engine = _setup_project(tmp_path)
        newer = engine.start("simple", {"task_name": "newer"})
        older = engine.start("simple", {"task_name": "older"})
        _backdate(tmp_path, newer["instance_id"], days=20)
        _backdate(tmp_path, older["instance_id"], days=40)

        result = check_enforcement(tmp_path)
        assert (
            f"You are in: simple @ start (instance {newer['instance_id']})"
            in result.guidance
        )
        assert (
            f"You are in: simple @ start (instance {older['instance_id']})"
            not in result.guidance
        )
        assert f"({older['instance_id']}, idle 40d)" in result.guidance


class TestGc:
    def test_gc_abandons_stale_keeps_fresh(self, tmp_path):
        engine = _setup_project(tmp_path)
        fresh = engine.start("simple", {"task_name": "fresh"})
        old = engine.start("simple", {"task_name": "old"})
        _backdate(tmp_path, old["instance_id"], days=30)

        result = gc_stale_instances(tmp_path)
        assert result["threshold_days"] == 14
        assert result["scanned"] == 2
        assert [a["instance_id"] for a in result["abandoned"]] == [
            old["instance_id"]
        ]

        remaining = engine._store.list_active()
        assert [i.instance_id for i in remaining] == [fresh["instance_id"]]
        # Abandoned instance is archived, not deleted
        abandoned_dir = tmp_path / ".process-state" / "abandoned"
        assert any(
            old["instance_id"] in p.name for p in abandoned_dir.rglob("*.json")
        )

    def test_gc_dry_run_moves_nothing(self, tmp_path):
        engine = _setup_project(tmp_path)
        old = engine.start("simple", {"task_name": "old"})
        _backdate(tmp_path, old["instance_id"], days=30)

        result = gc_stale_instances(tmp_path, dry_run=True)
        assert len(result["abandoned"]) == 1
        assert len(engine._store.list_active()) == 1

    def test_gc_override_threshold(self, tmp_path):
        engine = _setup_project(tmp_path)
        inst = engine.start("simple", {"task_name": "t"})
        _backdate(tmp_path, inst["instance_id"], days=5)
        assert gc_stale_instances(tmp_path)["abandoned"] == []
        result = gc_stale_instances(tmp_path, older_than_days=2)
        assert len(result["abandoned"]) == 1

    def test_gc_skips_suspended_parent(self, tmp_path):
        """A suspended parent's updated_at freezes at suspension while its
        child advances, so it goes 'stale' exactly while the child is live;
        gc must never collect it."""
        engine = _setup_project(tmp_path)
        inst = engine.start("simple", {"task_name": "parent"})
        active_dir = tmp_path / ".process-state" / "active"
        for path in active_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data["instance_id"] == inst["instance_id"]:
                data["suspended"] = True
                data["updated_at"] = (
                    datetime.now(timezone.utc) - timedelta(days=100)
                ).isoformat()
                path.write_text(json.dumps(data))
        result = gc_stale_instances(tmp_path)
        assert result["skipped_suspended"] == 1
        assert result["abandoned"] == []
        assert len(engine._store.list_active()) == 1

    def test_gc_emits_abandon_event(self, tmp_path):
        """gc routes through Engine.abandon so the event stream records
        the abandonment (no silent divergence from history)."""
        engine = _setup_project(tmp_path)
        old = engine.start("simple", {"task_name": "old"})
        _backdate(tmp_path, old["instance_id"], days=30)
        gc_stale_instances(tmp_path)
        events_path = tmp_path / ".process-state" / "events.jsonl"
        events = [
            json.loads(line)
            for line in events_path.read_text().splitlines()
            if line.strip()
        ]
        abandon_events = [
            e for e in events
            if e.get("event_type") == "abandon"
            and e.get("instance_id") == old["instance_id"]
        ]
        assert len(abandon_events) == 1

    def test_gc_rejects_non_positive_override(self, tmp_path):
        engine = _setup_project(tmp_path)
        inst = engine.start("simple", {"task_name": "t"})
        _backdate(tmp_path, inst["instance_id"], days=100)
        result = gc_stale_instances(tmp_path, older_than_days=0)
        assert result["abandoned"] == []
        assert "must be positive" in result["message"]
        result = gc_stale_instances(tmp_path, older_than_days=-3)
        assert result["abandoned"] == []
        assert "must be positive" in result["message"]

    def test_gc_skips_waiting(self, tmp_path):
        engine = _setup_project(tmp_path)
        inst = engine.start("simple", {"task_name": "w"})
        active_dir = tmp_path / ".process-state" / "active"
        for path in active_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data["instance_id"] == inst["instance_id"]:
                data["waiting"] = True
                data["updated_at"] = (
                    datetime.now(timezone.utc) - timedelta(days=100)
                ).isoformat()
                path.write_text(json.dumps(data))
        result = gc_stale_instances(tmp_path)
        assert result["skipped_waiting"] == 1
        assert result["abandoned"] == []

    def test_gc_disabled_at_zero(self, tmp_path):
        engine = _setup_project(tmp_path, settings={"stale_after_days": 0})
        inst = engine.start("simple", {"task_name": "t"})
        _backdate(tmp_path, inst["instance_id"], days=400)
        result = gc_stale_instances(tmp_path)
        assert result["abandoned"] == []
        assert "disabled" in result["message"]


WORKDIR_YAML = """
name: workdir
description: "Gate that reports its working directory"
version: "1.0.0"
states:
  - id: start
    type: initial
    transitions: [working]
  - id: working
    description: "work"
    transitions: [done]
    on_enter:
      validate:
        - command: "pwd"
          expect: not_empty
          message: "where am I"
  - id: done
    type: terminal
"""


class TestInstanceWorkDir:
    def _engine(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        (proc_dir / "workdir.yaml").write_text(WORKDIR_YAML)
        return Engine(tmp_path)

    async def test_gates_run_in_recorded_cwd(self, tmp_path):
        engine = self._engine(tmp_path)
        worktree = tmp_path / "worktree-a"
        worktree.mkdir()
        inst = engine.start("workdir", cwd=str(worktree))
        assert inst["project_dir"] == str(worktree.resolve())

        result = await engine.transition(inst["instance_id"], "working")
        assert result.success
        pwd_output = result.validation_results[0]["output"].strip()
        assert pwd_output == str(worktree.resolve())

    async def test_default_is_project_root(self, tmp_path):
        engine = self._engine(tmp_path)
        inst = engine.start("workdir")
        assert "project_dir" not in inst
        result = await engine.transition(inst["instance_id"], "working")
        pwd_output = result.validation_results[0]["output"].strip()
        assert pwd_output == str(tmp_path.resolve())

    async def test_missing_recorded_dir_falls_back(self, tmp_path):
        engine = self._engine(tmp_path)
        gone = tmp_path / "removed-worktree"
        gone.mkdir()
        inst = engine.start("workdir", cwd=str(gone))
        gone.rmdir()
        result = await engine.transition(inst["instance_id"], "working")
        assert result.success
        pwd_output = result.validation_results[0]["output"].strip()
        assert pwd_output == str(tmp_path.resolve())

    def test_status_reports_project_dir(self, tmp_path):
        engine = self._engine(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        inst = engine.start("workdir", cwd=str(worktree))
        info = engine.status(inst["instance_id"])
        assert info["project_dir"] == str(worktree.resolve())

    def test_status_list_reports_project_dir(self, tmp_path):
        """The no-arg status call (the post-compaction one) must carry
        project_dir too, not just the by-id lookup."""
        engine = self._engine(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        engine.start("workdir", cwd=str(worktree))
        entries = engine.status()
        assert entries[0]["project_dir"] == str(worktree.resolve())

    def test_start_rejects_relative_cwd(self, tmp_path):
        engine = self._engine(tmp_path)
        with pytest.raises(ValueError, match="absolute"):
            engine.start("workdir", cwd="../somewhere")

    def test_start_rejects_missing_cwd(self, tmp_path):
        engine = self._engine(tmp_path)
        with pytest.raises(ValueError, match="not an existing directory"):
            engine.start("workdir", cwd=str(tmp_path / "nope"))

    async def test_subprocess_child_inherits_project_dir(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        shutil.copy(
            FIXTURES / "subprocess-parent.yaml",
            proc_dir / "subprocess-parent.yaml",
        )
        shutil.copy(
            FIXTURES / "subprocess-child.yaml",
            proc_dir / "subprocess-child.yaml",
        )
        engine = Engine(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        parent = engine.start(
            "deploy-pipeline", {"deploy_target": "staging"},
            cwd=str(worktree),
        )
        await engine.transition(parent["instance_id"], "build")
        result = await engine.transition(parent["instance_id"], "test")
        child_id = result.subprocess_started
        assert child_id
        child = engine._store.load(child_id)
        assert child.project_dir == str(worktree.resolve())


AGENT_YAML = """
name: agented
description: "State that designates a custom agent"
version: "1.0.0"
states:
  - id: start
    type: initial
    transitions: [analyze]
  - id: analyze
    description: "analysis happens here"
    transitions: [done]
    agent_context:
      agent: process-designer
      model: opus
      fresh_context: true
      guidance: "Work only from the evidence file."
  - id: done
    type: terminal
"""


class TestAgentDesignation:
    def _engine(self, tmp_path, enforcement=None):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        (proc_dir / "agented.yaml").write_text(AGENT_YAML)
        if enforcement:
            (proc_dir / "registry.yaml").write_text(
                yaml.dump(
                    {"version": "1.0", "settings": {"enforcement": enforcement}}
                )
            )
        return Engine(tmp_path)

    async def test_transition_result_carries_agent_fields(self, tmp_path):
        engine = self._engine(tmp_path)
        inst = engine.start("agented")
        result = await engine.transition(inst["instance_id"], "analyze")
        assert result.agent_context["agent"] == "process-designer"
        assert result.agent_context["model"] == "opus"
        assert result.agent_context["fresh_context"] is True

    async def test_status_carries_agent_context(self, tmp_path):
        engine = self._engine(tmp_path)
        inst = engine.start("agented")
        await engine.transition(inst["instance_id"], "analyze")
        info = engine.status(inst["instance_id"])
        assert info["agent_context"]["agent"] == "process-designer"

    async def test_guard_guidance_names_agent(self, tmp_path):
        engine = self._engine(tmp_path, enforcement="monitor")
        inst = engine.start("agented")
        await engine.transition(inst["instance_id"], "analyze")
        result = check_enforcement(tmp_path)
        assert (
            "designated for the 'process-designer' agent "
            "(model: opus, fresh context)" in result.guidance
        )
