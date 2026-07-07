"""Tests for CI check commands (check_completed engine method)."""

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


def _complete_process(engine, params=None):
    """Helper: start and complete a simple process instance."""
    params = params or {"task_name": "test"}
    inst = engine.start("simple", params)
    pid = inst["instance_id"]

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine.transition(pid, "working"))
        loop.run_until_complete(engine.transition(pid, "review"))
        loop.run_until_complete(engine.transition(pid, "done"))
    finally:
        loop.close()
    return pid


class TestCheckCompleted:
    def test_no_instances_returns_fail(self, engine):
        result = engine.check_completed("simple")
        assert result["passed"] is False
        assert result["matches"] == []
        assert "No matching" in result["message"]

    def test_active_instance_not_found_by_default(self, engine):
        engine.start("simple", {"task_name": "wip"})
        result = engine.check_completed("simple")
        assert result["passed"] is False

    def test_active_instance_found_with_include_active(self, engine):
        engine.start("simple", {"task_name": "wip"})
        result = engine.check_completed("simple", include_active=True)
        assert result["passed"] is True
        assert len(result["matches"]) == 1

    def test_completed_instance_found(self, engine):
        _complete_process(engine)
        result = engine.check_completed("simple")
        assert result["passed"] is True
        assert len(result["matches"]) == 1
        assert result["matches"][0]["status"] == "completed"

    def test_filter_by_process_name(self, engine):
        _complete_process(engine)
        result = engine.check_completed("nonexistent")
        assert result["passed"] is False

    def test_filter_by_state_reached(self, engine):
        _complete_process(engine)
        # "review" was visited during the process
        result = engine.check_completed("simple", state="review")
        assert result["passed"] is True

    def test_filter_by_state_not_reached(self, engine):
        _complete_process(engine)
        # "never_existed" was never visited
        result = engine.check_completed("simple", state="never_existed")
        assert result["passed"] is False

    def test_filter_by_parameter(self, engine):
        _complete_process(engine, {"task_name": "deploy"})
        result = engine.check_completed(
            "simple", parameters={"task_name": "deploy"}
        )
        assert result["passed"] is True

    def test_filter_by_parameter_mismatch(self, engine):
        _complete_process(engine, {"task_name": "deploy"})
        result = engine.check_completed(
            "simple", parameters={"task_name": "other"}
        )
        assert result["passed"] is False

    def test_combined_filters(self, engine):
        _complete_process(engine, {"task_name": "deploy"})
        # Matching all criteria
        result = engine.check_completed(
            "simple",
            state="review",
            parameters={"task_name": "deploy"},
        )
        assert result["passed"] is True

    def test_combined_filters_partial_mismatch(self, engine):
        _complete_process(engine, {"task_name": "deploy"})
        # State matches but parameter doesn't
        result = engine.check_completed(
            "simple",
            state="review",
            parameters={"task_name": "wrong"},
        )
        assert result["passed"] is False

    def test_multiple_instances(self, engine):
        _complete_process(engine, {"task_name": "first"})
        _complete_process(engine, {"task_name": "second"})
        result = engine.check_completed("simple")
        assert result["passed"] is True
        assert len(result["matches"]) == 2

    def test_multiple_instances_filtered(self, engine):
        _complete_process(engine, {"task_name": "first"})
        _complete_process(engine, {"task_name": "second"})
        result = engine.check_completed(
            "simple", parameters={"task_name": "first"}
        )
        assert result["passed"] is True
        assert len(result["matches"]) == 1

    def test_message_includes_state(self, engine):
        result = engine.check_completed("simple", state="review")
        assert "review" in result["message"]

    def test_message_includes_parameters(self, engine):
        result = engine.check_completed(
            "simple", parameters={"branch": "main"}
        )
        assert "branch=main" in result["message"]

    def test_initial_state_is_visited(self, engine):
        _complete_process(engine)
        result = engine.check_completed("simple", state="start")
        assert result["passed"] is True

    def test_terminal_state_is_visited(self, engine):
        _complete_process(engine)
        result = engine.check_completed("simple", state="done")
        assert result["passed"] is True


class TestCheckCompletedPersistence:
    """Test that check_completed searches archived instances properly."""

    def test_searches_completed_dir(self, engine):
        pid = _complete_process(engine)
        # Instance should now be in completed/ dir, not active/
        result = engine.check_completed("simple")
        assert result["passed"] is True
        assert result["matches"][0]["instance_id"] == pid

    def test_abandoned_not_found(self, engine):
        inst = engine.start("simple", {"task_name": "test"})
        engine.abandon(inst["instance_id"], "test abandon")
        # Abandoned instances are not "completed"
        result = engine.check_completed("simple")
        assert result["passed"] is False
