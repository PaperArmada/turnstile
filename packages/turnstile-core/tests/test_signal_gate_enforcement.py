"""Tests that signal-driven transitions run validation gates.

A wait state is advanced with receive_signal, not transition. Before this was
fixed, receive_signal changed state without running any gates, so a gate on the
state after a wait was silently unenforced: an approval gate behind a
human-in-the-loop wait, exactly where enforcement matters most, did nothing.

These assert that a signal-driven transition is gated identically to an
ordinary one, through the engine's public surface and the persisted state.
"""

import asyncio
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
    shutil.copy(FIXTURES / "signal-gated.yaml", proc_dir / "signal-gated.yaml")
    return tmp_path


@pytest.fixture()
def engine(project: Path) -> Engine:
    return Engine(project)


def _persisted_state(project: Path, instance_id: str) -> str:
    return StateStore(project / ".process-state").load(instance_id).current_state


async def _arrive_at_wait(engine: Engine) -> str:
    iid = engine.start("signal-gated", {"task_name": "t"})["instance_id"]
    await engine.transition(iid, "awaiting")
    return iid


class TestSignalTransitionIsGated:
    @pytest.mark.asyncio
    async def test_failing_enter_gate_refuses_signal_transition(
        self, engine: Engine
    ):
        iid = await _arrive_at_wait(engine)

        result = await engine.receive_signal(
            iid, "reviewed", {"approver": "me"}, target_state="blocked_target"
        )

        assert result["success"] is False
        assert result["message"] == "on_enter validation failed"
        assert result["new_state"] == "awaiting"

    @pytest.mark.asyncio
    async def test_failing_enter_gate_does_not_persist_state(
        self, engine: Engine, project: Path
    ):
        iid = await _arrive_at_wait(engine)

        await engine.receive_signal(
            iid, "reviewed", {"approver": "me"}, target_state="blocked_target"
        )

        assert _persisted_state(project, iid) == "awaiting"

    @pytest.mark.asyncio
    async def test_failing_enter_gate_reports_the_failure(self, engine: Engine):
        iid = await _arrive_at_wait(engine)

        result = await engine.receive_signal(
            iid, "reviewed", {"approver": "me"}, target_state="blocked_target"
        )

        failed = [r for r in result["validation_results"] if not r["passed"]]
        assert failed, "the blocking gate must appear in validation_results"

    @pytest.mark.asyncio
    async def test_signal_is_recorded_even_when_transition_refused(
        self, engine: Engine, project: Path
    ):
        """The signal genuinely arrived; discarding it would lose a fact.

        The instance stops waiting so it is not stranded, but the state does
        not advance past the failing gate.
        """
        iid = await _arrive_at_wait(engine)

        await engine.receive_signal(
            iid, "reviewed", {"approver": "me"}, target_state="blocked_target"
        )

        instance = StateStore(project / ".process-state").load(iid)
        assert instance.waiting is False
        assert instance.signal_data == {"approver": "me"}

    @pytest.mark.asyncio
    async def test_passing_gate_allows_signal_transition(
        self, engine: Engine, project: Path
    ):
        iid = await _arrive_at_wait(engine)

        result = await engine.receive_signal(
            iid, "reviewed", {"approver": "me"}, target_state="open_target"
        )

        assert result["success"] is True
        assert result["new_state"] == "open_target"
        assert _persisted_state(project, iid) == "open_target"


class TestSignalWithoutTargetIsUnaffected:
    @pytest.mark.asyncio
    async def test_signal_without_target_does_not_run_target_gates(
        self, engine: Engine, project: Path
    ):
        """Delivering a signal without a target only clears the wait.

        There is no target state, so no gate should run and no failure should
        be reported; the instance simply becomes transitionable.
        """
        iid = await _arrive_at_wait(engine)

        result = await engine.receive_signal(iid, "reviewed", {"approver": "me"})

        assert result["success"] is True
        assert result["new_state"] == "awaiting"
        assert "validation_results" not in result
        assert _persisted_state(project, iid) == "awaiting"
