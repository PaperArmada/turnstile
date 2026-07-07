"""Tests for turnstile_core.instance.analytics."""

import shutil
from pathlib import Path

import pytest

from turnstile_core.runtime.engine import Engine

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def engine(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
    return Engine(tmp_path)


async def _complete_process(engine, params=None):
    """Start and complete a simple process."""
    params = params or {"task_name": "test"}
    inst = engine.start("simple", params)
    pid = inst["instance_id"]
    await engine.transition(pid, "working")
    await engine.transition(pid, "review")
    await engine.transition(pid, "done")
    return pid


class TestAnalytics:
    def test_empty_analytics(self, engine):
        result = engine.analytics()
        assert result["total_instances"] == 0
        assert result["processes"] == {}

    @pytest.mark.asyncio
    async def test_single_completed(self, engine):
        await _complete_process(engine)
        result = engine.analytics()
        assert result["total_instances"] == 1
        assert "simple" in result["processes"]

        stats = result["processes"]["simple"]
        assert stats["completed"] == 1
        assert stats["abandoned"] == 0
        assert stats["completion_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_completion_rate(self, engine):
        await _complete_process(engine)
        # Abandon one
        inst = engine.start("simple", {"task_name": "abandon"})
        engine.abandon(inst["instance_id"], "test")

        result = engine.analytics()
        stats = result["processes"]["simple"]
        assert stats["completed"] == 1
        assert stats["abandoned"] == 1
        assert stats["completion_rate"] == 0.5

    @pytest.mark.asyncio
    async def test_avg_duration(self, engine):
        await _complete_process(engine)
        result = engine.analytics()
        stats = result["processes"]["simple"]
        assert stats["avg_duration_seconds"] is not None
        assert stats["avg_duration_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_state_durations(self, engine):
        await _complete_process(engine)
        result = engine.analytics()
        state_avg = result["processes"]["simple"]["state_avg_duration_seconds"]
        # States visited: start, working, review
        assert "start" in state_avg
        assert "working" in state_avg
        assert "review" in state_avg

    @pytest.mark.asyncio
    async def test_override_patterns(self, engine):
        inst = engine.start("simple", {"task_name": "test"})
        pid = inst["instance_id"]
        await engine.skip(pid, "done", "test override")

        result = engine.analytics()
        stats = result["processes"]["simple"]
        assert stats["total_overrides"] == 1
        assert "start -> done" in stats["override_patterns"]

    @pytest.mark.asyncio
    async def test_multiple_processes(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
        shutil.copy(
            FIXTURES / "subprocess-child.yaml",
            proc_dir / "integration-tests.yaml",
        )
        eng = Engine(tmp_path)

        # Complete simple
        await _complete_process(eng)

        # Complete integration-tests
        inst = eng.start("integration-tests", {"target_env": "staging"})
        cid = inst["instance_id"]
        await eng.transition(cid, "run")
        await eng.transition(cid, "done")

        result = eng.analytics()
        assert result["total_instances"] == 2
        assert "simple" in result["processes"]
        assert "integration-tests" in result["processes"]

    @pytest.mark.asyncio
    async def test_multiple_completed_instances(self, engine):
        await _complete_process(engine, {"task_name": "first"})
        await _complete_process(engine, {"task_name": "second"})
        await _complete_process(engine, {"task_name": "third"})

        result = engine.analytics()
        stats = result["processes"]["simple"]
        assert stats["total"] == 3
        assert stats["completed"] == 3

    @pytest.mark.asyncio
    async def test_engine_analytics_method(self, engine):
        await _complete_process(engine)
        result = engine.analytics()
        assert "processes" in result
        assert "total_instances" in result
