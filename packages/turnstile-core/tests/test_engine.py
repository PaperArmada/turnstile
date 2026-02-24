"""Tests for turnstile_core.engine."""

import shutil
from pathlib import Path

import pytest

from turnstile_core.engine import Engine
from turnstile_core.errors import (
    DefinitionError,
    InstanceNotFoundError,
    ProcessNotFoundError,
    TransitionError,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def project(tmp_path) -> Path:
    """Create a temporary project with the simple process definition."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
    return tmp_path


@pytest.fixture()
def engine(project: Path) -> Engine:
    return Engine(project)


class TestListProcesses:
    def test_lists_discovered(self, engine: Engine):
        procs = engine.list_processes()
        assert len(procs) == 1
        assert procs[0]["name"] == "simple"
        assert procs[0]["version"] == "1.0.0"

    def test_empty_project(self, tmp_path):
        e = Engine(tmp_path)
        assert e.list_processes() == []


class TestStart:
    def test_start_process(self, engine: Engine):
        result = engine.start("simple", {"task_name": "build feature"})
        assert result["process_name"] == "simple"
        assert result["current_state"] == "start"
        assert result["available_transitions"] == ["working"]
        assert result["parameters"]["task_name"] == "build feature"
        assert result["instance_id"]

    def test_missing_required_param(self, engine: Engine):
        with pytest.raises(DefinitionError, match="Required parameter"):
            engine.start("simple")

    def test_unknown_process(self, engine: Engine):
        with pytest.raises(ProcessNotFoundError):
            engine.start("nonexistent")


class TestTransition:
    @pytest.mark.asyncio
    async def test_legal_transition(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        result = await engine.transition(iid, "working")
        assert result.success is True
        assert result.new_state == "working"
        assert "review" in result.available_transitions

    @pytest.mark.asyncio
    async def test_illegal_transition(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        with pytest.raises(TransitionError, match="not allowed"):
            await engine.transition(iid, "done")

    @pytest.mark.asyncio
    async def test_full_path(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(iid, "working")
        await engine.transition(iid, "review")
        result = await engine.transition(iid, "done")

        assert result.success is True
        assert result.new_state == "done"
        assert result.available_transitions == []

    @pytest.mark.asyncio
    async def test_backward_transition(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(iid, "working")
        await engine.transition(iid, "review")
        # Go back to working
        result = await engine.transition(iid, "working")
        assert result.success is True
        assert result.new_state == "working"

    @pytest.mark.asyncio
    async def test_validation_runs(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        # Transition to working, then to review (triggers on_exit of working
        # and on_enter of review)
        await engine.transition(iid, "working")
        result = await engine.transition(iid, "review")
        assert result.success is True
        assert len(result.validation_results) > 0


class TestStatus:
    def test_single_instance(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        status = engine.status(started["instance_id"])
        assert status["current_state"] == "start"
        assert status["process_name"] == "simple"

    def test_all_instances(self, engine: Engine):
        engine.start("simple", {"task_name": "a"})
        engine.start("simple", {"task_name": "b"})
        statuses = engine.status()
        assert len(statuses) == 2

    def test_missing_instance(self, engine: Engine):
        with pytest.raises(InstanceNotFoundError):
            engine.status("nonexistent")


class TestSkip:
    @pytest.mark.asyncio
    async def test_skip_to_state(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        result = await engine.skip(iid, "review", "urgent hotfix")
        assert result.success is True
        assert result.new_state == "review"
        assert "Override logged" in result.message

    @pytest.mark.asyncio
    async def test_skip_requires_reason(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        with pytest.raises(TransitionError, match="reason"):
            await engine.skip(iid, "review", "")


class TestAbandon:
    def test_abandon(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        result = engine.abandon(iid, "no longer needed")
        assert result["success"] is True
        assert result["final_state"] == "start"

        # Should no longer be active
        assert engine.status() == []


class TestUndo:
    @pytest.mark.asyncio
    async def test_undo_last(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(iid, "working")
        result = engine.undo(iid, "premature transition")
        assert result.success is True
        assert result.new_state == "start"

    def test_undo_no_history(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        with pytest.raises(TransitionError, match="No transitions"):
            engine.undo(iid, "nothing to undo")

    @pytest.mark.asyncio
    async def test_cannot_undo_skip(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.skip(iid, "review", "testing skip")
        with pytest.raises(TransitionError, match="Cannot undo a skip"):
            engine.undo(iid, "trying to undo skip")


class TestHandoff:
    def test_handoff(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        result = engine.handoff(iid, "alice", "out of office")
        assert result["success"] is True
        assert result["new_owner"] == "alice"


class TestHistory:
    @pytest.mark.asyncio
    async def test_history(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(iid, "working")
        await engine.transition(iid, "review")

        hist = engine.history(iid)
        assert len(hist) == 2
        assert hist[0]["from_state"] == "start"
        assert hist[0]["to_state"] == "working"
        assert hist[1]["from_state"] == "working"
        assert hist[1]["to_state"] == "review"

    @pytest.mark.asyncio
    async def test_history_completed_instance(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(iid, "working")
        await engine.transition(iid, "review")
        await engine.transition(iid, "done")

        # Instance is now archived, but history should still work
        hist = engine.history(iid)
        assert len(hist) == 3
        assert hist[-1]["to_state"] == "done"


class TestValidateDefinition:
    def test_valid(self, engine: Engine):
        result = engine.validate_definition(str(FIXTURES / "simple.yaml"))
        assert result["valid"] is True
        assert "simple" == result["name"]

    def test_invalid(self, engine: Engine, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("name: bad\nstates: []\n")
        result = engine.validate_definition(str(bad))
        assert result["valid"] is False
        assert len(result["errors"]) > 0


class TestTerminalCompletion:
    @pytest.mark.asyncio
    async def test_terminal_moves_to_completed(self, engine: Engine, project: Path):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(iid, "working")
        await engine.transition(iid, "review")
        await engine.transition(iid, "done")

        # No longer active
        assert engine.status() == []

        # Should exist in completed/
        state_dir = project / ".process-state"
        completed_files = list(state_dir.joinpath("completed").rglob("*.json"))
        assert len(completed_files) == 1
