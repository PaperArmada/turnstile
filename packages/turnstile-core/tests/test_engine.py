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


class TestInfo:
    def test_returns_parameters(self, engine: Engine):
        result = engine.info("simple")
        assert result["name"] == "simple"
        assert result["version"] == "1.0.0"
        assert len(result["parameters"]) >= 1
        param = result["parameters"][0]
        assert param["name"] == "task_name"
        assert param["required"] is True

    def test_returns_states(self, engine: Engine):
        result = engine.info("simple")
        states = result["states"]
        state_ids = [s["id"] for s in states]
        assert "start" in state_ids
        assert "working" in state_ids
        assert "done" in state_ids

    def test_state_types(self, engine: Engine):
        result = engine.info("simple")
        states = {s["id"]: s for s in result["states"]}
        assert states["start"]["type"] == "initial"
        assert states["done"]["type"] == "terminal"
        assert states["working"]["type"] == "normal"

    def test_state_transitions(self, engine: Engine):
        result = engine.info("simple")
        states = {s["id"]: s for s in result["states"]}
        assert "working" in states["start"]["transitions"]

    def test_unknown_process(self, engine: Engine):
        with pytest.raises(ProcessNotFoundError):
            engine.info("nonexistent")


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


class TestActions:
    """Tests for on_enter and on_exit action execution."""

    @pytest.fixture()
    def actions_project(self, tmp_path) -> Path:
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        shutil.copy(FIXTURES / "actions.yaml", proc_dir / "actions.yaml")
        return tmp_path

    @pytest.fixture()
    def actions_engine(self, actions_project: Path) -> Engine:
        return Engine(actions_project)

    @pytest.mark.asyncio
    async def test_on_exit_actions_run(self, actions_engine: Engine, tmp_path: Path):
        """on_exit actions should execute when leaving a state."""
        marker_dir = tmp_path / "markers"
        marker_dir.mkdir()

        started = actions_engine.start("actions", {"marker_dir": str(marker_dir)})
        iid = started["instance_id"]

        await actions_engine.transition(iid, "working")

        assert (marker_dir / "exit_start").exists(), "on_exit action for 'start' did not run"
        assert (marker_dir / "enter_working").exists(), "on_enter action for 'working' did not run"

    @pytest.mark.asyncio
    async def test_on_exit_actions_run_before_on_enter(self, actions_engine: Engine, tmp_path: Path):
        """on_exit actions run before on_enter actions (exit current, then enter target)."""
        marker_dir = tmp_path / "markers"
        marker_dir.mkdir()

        started = actions_engine.start("actions", {"marker_dir": str(marker_dir)})
        iid = started["instance_id"]

        await actions_engine.transition(iid, "working")

        exit_time = (marker_dir / "exit_start").stat().st_mtime_ns
        enter_time = (marker_dir / "enter_working").stat().st_mtime_ns
        assert exit_time <= enter_time

    @pytest.mark.asyncio
    async def test_on_exit_actions_full_path(self, actions_engine: Engine, tmp_path: Path):
        """on_exit and on_enter actions fire at every transition."""
        marker_dir = tmp_path / "markers"
        marker_dir.mkdir()

        started = actions_engine.start("actions", {"marker_dir": str(marker_dir)})
        iid = started["instance_id"]

        await actions_engine.transition(iid, "working")
        await actions_engine.transition(iid, "done")

        assert (marker_dir / "exit_start").exists()
        assert (marker_dir / "enter_working").exists()
        assert (marker_dir / "exit_working").exists()
        assert (marker_dir / "enter_done").exists()


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
    async def test_skip_records_reason_in_history(self, engine: Engine):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.skip(iid, "review", "urgent hotfix")
        history = engine.history(iid)
        skip_entry = history[-1]
        assert skip_entry["triggered_by"] == "skip: urgent hotfix"

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

    @pytest.mark.asyncio
    async def test_transition_metadata(self, engine: Engine):
        """Metadata dict is stored in history and surfaced by history()."""
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(
            iid, "working", metadata={"cluster_id": "valuation", "iteration": "1"}
        )
        await engine.transition(iid, "review")

        hist = engine.history(iid)
        assert hist[0]["metadata"] == {"cluster_id": "valuation", "iteration": "1"}
        # Second transition has no metadata, should not appear in response
        assert "metadata" not in hist[1]

    @pytest.mark.asyncio
    async def test_transition_metadata_none(self, engine: Engine):
        """Omitting metadata produces an empty dict (backward compatible)."""
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(iid, "working")

        hist = engine.history(iid)
        assert "metadata" not in hist[0]

    @pytest.mark.asyncio
    async def test_metadata_persists_through_completion(self, engine: Engine):
        """Metadata survives archival to completed/."""
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(iid, "working", metadata={"reason": "hotfix"})
        await engine.transition(iid, "review")
        await engine.transition(iid, "done")

        hist = engine.history(iid)
        assert hist[0]["metadata"] == {"reason": "hotfix"}


class TestRequiredMetadata:
    """Test required_metadata enforcement on transitions."""

    @pytest.fixture()
    def rm_engine(self, tmp_path) -> Engine:
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        shutil.copy(
            FIXTURES / "required-metadata.yaml",
            proc_dir / "required-metadata.yaml",
        )
        return Engine(tmp_path)

    @pytest.mark.asyncio
    async def test_missing_metadata_raises(self, rm_engine: Engine):
        """Transition without required metadata raises TransitionError."""
        started = rm_engine.start("required-metadata", {"task_name": "test"})
        iid = started["instance_id"]

        with pytest.raises(TransitionError, match="requires metadata"):
            await rm_engine.transition(iid, "review")

    @pytest.mark.asyncio
    async def test_partial_metadata_raises(self, rm_engine: Engine):
        """Providing only some required keys raises TransitionError."""
        started = rm_engine.start("required-metadata", {"task_name": "test"})
        iid = started["instance_id"]

        with pytest.raises(TransitionError, match="'confidence'"):
            await rm_engine.transition(
                iid, "review", metadata={"summary": "did the thing"}
            )

    @pytest.mark.asyncio
    async def test_all_metadata_allows_transition(self, rm_engine: Engine):
        """Providing all required keys allows the transition."""
        started = rm_engine.start("required-metadata", {"task_name": "test"})
        iid = started["instance_id"]

        result = await rm_engine.transition(
            iid, "review",
            metadata={"summary": "implemented feature", "confidence": "high"},
        )
        assert result.success is True
        assert result.new_state == "review"

    @pytest.mark.asyncio
    async def test_metadata_stored_in_history(self, rm_engine: Engine):
        """Required metadata is persisted in history."""
        started = rm_engine.start("required-metadata", {"task_name": "test"})
        iid = started["instance_id"]

        await rm_engine.transition(
            iid, "review",
            metadata={"summary": "the fix", "confidence": "high"},
        )

        hist = rm_engine.history(iid)
        assert hist[0]["metadata"]["summary"] == "the fix"
        assert hist[0]["metadata"]["confidence"] == "high"

    @pytest.mark.asyncio
    async def test_no_required_metadata_state_ok(self, rm_engine: Engine):
        """States without required_metadata don't enforce."""
        started = rm_engine.start("required-metadata", {"task_name": "test"})
        iid = started["instance_id"]

        await rm_engine.transition(
            iid, "review",
            metadata={"summary": "done", "confidence": "high"},
        )
        # review has no required_metadata, so transitioning without metadata is fine
        result = await rm_engine.transition(iid, "done")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_required_metadata_in_transition_result(self, rm_engine: Engine):
        """TransitionResult includes required_metadata for the target state."""
        started = rm_engine.start("required-metadata", {"task_name": "test"})
        iid = started["instance_id"]

        # start has required_metadata; transitioning to review should
        # include review's required_metadata (which is empty) in the result
        result = await rm_engine.transition(
            iid, "review",
            metadata={"summary": "done", "confidence": "high"},
        )
        assert result.required_metadata == []

    def test_required_metadata_in_status(self, rm_engine: Engine):
        """Status shows required_metadata for the current state."""
        started = rm_engine.start("required-metadata", {"task_name": "test"})
        iid = started["instance_id"]

        status = rm_engine.status(iid)
        assert "required_metadata" in status
        keys = [rm["key"] for rm in status["required_metadata"]]
        assert "summary" in keys
        assert "confidence" in keys

    def test_required_metadata_in_info(self, rm_engine: Engine):
        """process_info surfaces required_metadata on states."""
        info = rm_engine.info("required-metadata")
        start_state = [s for s in info["states"] if s["id"] == "start"][0]
        assert "required_metadata" in start_state
        assert len(start_state["required_metadata"]) == 2

        # review has no required_metadata, so it shouldn't appear
        review_state = [s for s in info["states"] if s["id"] == "review"][0]
        assert "required_metadata" not in review_state


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


class TestTerminalSummary:
    @pytest.mark.asyncio
    async def test_summary_on_terminal(self, engine: Engine, project: Path):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(iid, "working")
        await engine.transition(iid, "review")
        result = await engine.transition(iid, "done")

        assert result.summary is not None
        assert result.summary["transition_count"] == 3
        assert "start" in result.summary["states_visited"]
        assert "done" in result.summary["states_visited"]
        assert result.summary["override_count"] == 0
        assert result.summary["elapsed"] != "unknown"

    @pytest.mark.asyncio
    async def test_no_summary_on_non_terminal(self, engine: Engine, project: Path):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        result = await engine.transition(iid, "working")
        assert result.summary is None

    @pytest.mark.asyncio
    async def test_summary_counts_overrides(self, engine: Engine, project: Path):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(iid, "working")
        await engine.skip(iid, "done", "testing summary with skip")

        # Load from completed to check, since skip to terminal completes it
        history = engine.history(iid)
        assert any("skip" in (h.get("triggered_by") or "") for h in history)

    @pytest.mark.asyncio
    async def test_summary_validation_counts(self, engine: Engine, project: Path):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        await engine.transition(iid, "working")
        await engine.transition(iid, "review")
        result = await engine.transition(iid, "done")

        summary = result.summary
        # Validation counts should be non-negative
        assert summary["validations_run"] >= 0
        assert summary["validations_passed"] >= 0
        assert summary["validations_failed"] >= 0
        assert summary["validations_run"] == (
            summary["validations_passed"] + summary["validations_failed"]
        )
