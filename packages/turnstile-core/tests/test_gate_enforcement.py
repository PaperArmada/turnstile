"""Tests that a blocking validation gate actually stops a transition.

These are deliberately end-to-end through the Engine rather than unit tests of
`has_blocking_failures`. The distinction matters: the helper itself is covered
in test_validator.py, but before this module existed the engine could ignore
its result entirely and the whole suite still passed. Gate blocking is the
behavior the project's central claim rests on, so it is asserted here against
the engine's public surface, including the state persisted to disk.
"""

import shutil
from pathlib import Path

import pytest

from turnstile_core.engine import Engine
from turnstile_core.persistence import StateStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def project(tmp_path) -> Path:
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(FIXTURES / "gated.yaml", proc_dir / "gated.yaml")
    return tmp_path


@pytest.fixture()
def engine(project: Path) -> Engine:
    return Engine(project)


def _persisted_state(project: Path, instance_id: str) -> str:
    """Read the instance's current state back from disk.

    Asserting on the returned object alone would not catch an engine that
    refuses the transition in its return value but still writes the new state.
    """
    store = StateStore(project / ".process-state")
    return store.load(instance_id).current_state


class TestOnEnterGateBlocks:
    @pytest.mark.asyncio
    async def test_failing_on_enter_gate_refuses_transition(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("gated", {"task_name": "t"})["instance_id"]

        result = await engine.transition(iid, "enter_blocked")

        assert result.success is False
        assert result.message == "on_enter validation failed"
        assert result.new_state == "start"

    @pytest.mark.asyncio
    async def test_failing_on_enter_gate_does_not_persist_state(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("gated", {"task_name": "t"})["instance_id"]

        await engine.transition(iid, "enter_blocked")

        assert _persisted_state(project, iid) == "start"

    @pytest.mark.asyncio
    async def test_failing_on_enter_gate_records_no_history(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("gated", {"task_name": "t"})["instance_id"]

        await engine.transition(iid, "enter_blocked")

        store = StateStore(project / ".process-state")
        assert store.load(iid).history == []

    @pytest.mark.asyncio
    async def test_blocked_transition_surfaces_the_failure(
        self, engine: Engine
    ):
        iid = engine.start("gated", {"task_name": "t"})["instance_id"]

        result = await engine.transition(iid, "enter_blocked")

        failed = [r for r in result.validation_results if not r["passed"]]
        assert failed, "the blocking gate must appear in validation_results"
        assert any(
            "on_enter gate must block entry" in r.get("message", "")
            for r in failed
        )


class TestOnExitGateBlocks:
    @pytest.mark.asyncio
    async def test_failing_on_exit_gate_refuses_transition(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("gated", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "exit_blocked")
        assert _persisted_state(project, iid) == "exit_blocked"

        result = await engine.transition(iid, "open")

        assert result.success is False
        assert result.message == "on_exit validation failed"
        assert result.new_state == "exit_blocked"

    @pytest.mark.asyncio
    async def test_failing_on_exit_gate_does_not_persist_state(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("gated", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "exit_blocked")

        await engine.transition(iid, "open")

        assert _persisted_state(project, iid) == "exit_blocked"

    @pytest.mark.asyncio
    async def test_instance_stays_usable_after_a_blocked_exit(
        self, engine: Engine, project: Path
    ):
        """A refused transition must not wedge the instance."""
        iid = engine.start("gated", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "exit_blocked")
        await engine.transition(iid, "open")

        status = engine.status(iid)
        assert status["current_state"] == "exit_blocked"
        assert "open" in status["available_transitions"]


class TestNonBlockingSeverity:
    @pytest.mark.asyncio
    async def test_failing_warning_gate_allows_transition(
        self, engine: Engine, project: Path
    ):
        """severity=warning must surface the failure without blocking.

        This is the counterpart to the blocking tests: it pins the boundary so
        a fix that simply blocks on *any* gate failure is also caught.
        """
        iid = engine.start("gated", {"task_name": "t"})["instance_id"]

        result = await engine.transition(iid, "warn_only")

        assert result.success is True
        assert result.new_state == "warn_only"
        assert _persisted_state(project, iid) == "warn_only"

    @pytest.mark.asyncio
    async def test_failing_warning_gate_is_still_reported(
        self, engine: Engine
    ):
        iid = engine.start("gated", {"task_name": "t"})["instance_id"]

        result = await engine.transition(iid, "warn_only")

        failed = [r for r in result.validation_results if not r["passed"]]
        assert failed, "a failing warning gate must still be reported"


class TestUngatedTransitionStillWorks:
    @pytest.mark.asyncio
    async def test_ungated_transition_succeeds(
        self, engine: Engine, project: Path
    ):
        """Guards against a regression that blocks everything indiscriminately."""
        iid = engine.start("gated", {"task_name": "t"})["instance_id"]

        result = await engine.transition(iid, "open")

        assert result.success is True
        assert _persisted_state(project, iid) == "open"
