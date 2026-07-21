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
    async def test_refused_signal_is_a_no_op_and_stays_waiting(
        self, engine: Engine, project: Path
    ):
        """A blocked signal transition leaves the instance untouched.

        Mirroring a blocked ordinary transition, the wait is NOT cleared and
        no state is written, so the same signal can be re-delivered once the
        gate condition is met (see the recovery test below).
        """
        iid = await _arrive_at_wait(engine)

        result = await engine.receive_signal(
            iid, "reviewed", {"approver": "me"}, target_state="blocked_target"
        )
        assert result["still_waiting"] is True

        instance = StateStore(project / ".process-state").load(iid)
        assert instance.waiting is True
        assert instance.current_state == "awaiting"

    @pytest.mark.asyncio
    async def test_signal_can_be_redelivered_after_gate_passes(
        self, engine: Engine, project: Path
    ):
        """The central recovery claim: refuse, fix the gate, re-deliver.

        The gate on gated_on_param reads an env-passed parameter so the test
        can flip it from failing to passing between the two deliveries, then
        assert the re-delivered signal transitions AND carries attribution.
        """
        iid = engine.start("signal-gated", {"task_name": "t", "gate_ok": "no"})[
            "instance_id"
        ]
        await engine.transition(iid, "awaiting")

        refused = await engine.receive_signal(
            iid, "reviewed", {"approver": "alice"}, target_state="gated_on_param"
        )
        assert refused["success"] is False

        # Fix the condition the gate checks, then re-deliver the same signal.
        inst = engine._store.load(iid)
        inst.parameters["gate_ok"] = "yes"
        engine._store.save(inst)

        ok = await engine.receive_signal(
            iid, "reviewed", {"approver": "alice"}, target_state="gated_on_param"
        )
        assert ok["success"] is True
        assert ok["new_state"] == "gated_on_param"

        # Attribution survives on the transition that actually advanced.
        instance = engine._store.load(iid)
        last = instance.history[-1]
        assert last.triggered_by == "signal: reviewed"
        assert last.metadata["signal_data"] == {"approver": "alice"}

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


class TestSignalGatePathCoverage:
    @pytest.mark.asyncio
    async def test_failing_on_exit_gate_refuses_signal_transition(
        self, engine: Engine, project: Path
    ):
        """The on_exit branch of the signal path blocks departure."""
        iid = engine.start("signal-gated", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "awaiting_exit_gated")

        result = await engine.receive_signal(
            iid, "reviewed", {"approver": "me"}, target_state="open_target"
        )

        assert result["success"] is False
        assert result["message"] == "on_exit validation failed"
        assert _persisted_state(project, iid) == "awaiting_exit_gated"

    @pytest.mark.asyncio
    async def test_gated_terminal_target_is_refused(
        self, engine: Engine, project: Path
    ):
        """A terminal reached via signal is gated like any other target."""
        iid = await _arrive_at_wait(engine)

        result = await engine.receive_signal(
            iid, "reviewed", {"approver": "me"}, target_state="blocked_terminal"
        )

        assert result["success"] is False
        assert _persisted_state(project, iid) == "awaiting"


class TestSignalRunsActions:
    @pytest.mark.asyncio
    async def test_signal_transition_runs_on_enter_actions(
        self, engine: Engine, project: Path
    ):
        """Parity with transition: a signal-driven entry runs on_enter actions.

        open_action's on_enter action touches a marker file; reaching it via
        signal must run it, just as an ordinary transition would.
        """
        iid = await _arrive_at_wait(engine)

        await engine.receive_signal(
            iid, "reviewed", {"approver": "me"}, target_state="open_action"
        )

        assert (project / "SIGNAL_ACTION_RAN").exists()


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
