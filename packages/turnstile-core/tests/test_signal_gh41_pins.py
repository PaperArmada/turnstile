"""Regression pins for the signal-path asymmetries tracked by GH #41.

The #33 refactor (5f1daea) unified state entry in ``Engine._enter_state``
but deliberately preserved three pre-existing behaviors behind
``via_signal`` guards rather than silently changing the contract:

1. A subprocess child completed via ``receive_signal`` does NOT resume
   its suspended parent and does NOT fire the on_complete notification.
2. A signal targeting a wait state lands there with waiting cleared and
   transitions unlocked (the second wait state does not wait).
3. A signal targeting a subprocess-type state does not start a child.

Every test here pins CURRENT behavior per GH-41. When #41 decides the
contract, FLIP the affected tests instead of deleting them.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from turnstile_core.engine import Engine
from turnstile_core.errors import TransitionError

FIXTURES = Path(__file__).parent / "fixtures"


# A child whose terminal state is reachable from a wait state, so it can
# be completed either via receive_signal (the pinned path) or via a plain
# transition after a target-less signal (the control path).
SIGNAL_CHILD_PROCESS = {
    "name": "signal-child",
    "description": "Child completable from a wait state",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["await_result"]},
        {
            "id": "await_result",
            "type": "wait",
            "signal": {
                "name": "result_ready",
                "required_fields": [{"key": "outcome"}],
            },
            "transitions": ["done"],
        },
        {"id": "done", "type": "terminal"},
    ],
}

SIGNAL_PARENT_PROCESS = {
    "name": "signal-parent",
    "description": "Parent that delegates to signal-child",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["delegate"]},
        {
            "id": "delegate",
            "type": "subprocess",
            "process": "signal-child",
            "subprocess_routing": {
                "on_complete": ["wrap_up"],
                "on_fail": ["wrap_up"],
            },
        },
        {"id": "wrap_up", "transitions": ["finished"]},
        {"id": "finished", "type": "terminal"},
    ],
}

DOUBLE_WAIT_PROCESS = {
    "name": "double-wait",
    "description": "Wait state whose signal target is another wait state",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["first_gate"]},
        {
            "id": "first_gate",
            "type": "wait",
            "signal": {
                "name": "go",
                "required_fields": [{"key": "note"}],
            },
            "transitions": ["second_gate"],
        },
        {
            "id": "second_gate",
            "type": "wait",
            "signal": {"name": "confirm"},
            "transitions": ["end"],
        },
        {"id": "end", "type": "terminal"},
    ],
}

SIGNAL_SUBPROCESS_TARGET_PROCESS = {
    "name": "signal-subprocess-target",
    "description": "Wait state whose signal target is a subprocess state",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["gate"]},
        {
            "id": "gate",
            "type": "wait",
            "signal": {"name": "proceed"},
            "transitions": ["delegate"],
        },
        {
            "id": "delegate",
            "type": "subprocess",
            "process": "signal-child",
            "subprocess_routing": {
                "on_complete": ["end"],
                "on_fail": ["end"],
            },
        },
        {"id": "end", "type": "terminal"},
    ],
}


@pytest.fixture()
def project(tmp_path) -> Path:
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "signal-child.yaml").write_text(
        yaml.dump(SIGNAL_CHILD_PROCESS)
    )
    (proc_dir / "signal-parent.yaml").write_text(
        yaml.dump(SIGNAL_PARENT_PROCESS)
    )
    (proc_dir / "double-wait.yaml").write_text(yaml.dump(DOUBLE_WAIT_PROCESS))
    (proc_dir / "signal-subprocess-target.yaml").write_text(
        yaml.dump(SIGNAL_SUBPROCESS_TARGET_PROCESS)
    )
    return tmp_path


@pytest.fixture()
def engine(project: Path) -> Engine:
    return Engine(project)


def _events_for(project: Path, instance_id: str) -> list[dict[str, Any]]:
    path = project / ".process-state" / "events.jsonl"
    assert path.exists(), "events.jsonl was not created"
    return [
        e
        for e in (json.loads(line) for line in path.read_text().splitlines())
        if e["instance_id"] == instance_id
    ]


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [e["event_type"] for e in events]


async def _suspended_parent_with_waiting_child(
    engine: Engine,
) -> tuple[str, str]:
    """Parent suspended on its subprocess state; child in its wait state."""
    pid = engine.start("signal-parent")["instance_id"]
    result = await engine.transition(pid, "delegate")
    child_id = result.subprocess_started
    assert child_id is not None
    await engine.transition(child_id, "await_result")
    return pid, child_id


class TestSignalCompletionDoesNotResumeParent:
    """GH-41 pin: terminal entry via signal skips parent resume."""

    async def test_child_completed_by_signal_leaves_parent_suspended(
        self, engine: Engine
    ):
        # Pins current behavior per GH-41: completing a subprocess child
        # through receive_signal does NOT resume the suspended parent
        # (the same completion via transition() does; see
        # test_subprocess.py::test_child_completion_resumes_parent).
        # FLIP this test when #41 resolves.
        pid, child_id = await _suspended_parent_with_waiting_child(engine)

        result = await engine.receive_signal(
            child_id,
            "result_ready",
            {"outcome": "pass"},
            target_state="done",
        )

        assert result["success"] is True
        assert result["new_state"] == "done"
        assert "parent_resumed" not in result

        parent_status = engine.status(pid)
        assert parent_status["suspended"] is True
        assert parent_status["current_state"] == "delegate"
        assert parent_status["available_transitions"] == []

    async def test_child_completed_by_signal_fires_no_on_complete_notification(
        self, project: Path
    ):
        # Pins current behavior per GH-41: terminal entry via signal does
        # not fire the on_complete notification. FLIP when #41 resolves.
        # The second half of the test is a control: the same process
        # completed via transition() DOES notify, so the negative
        # assertion cannot pass vacuously (e.g. misconfigured registry).
        log_file = project / "notifications.log"
        registry = project / ".processes" / "registry.yaml"
        registry.write_text(
            'version: "1.0"\n'
            "settings:\n"
            "  notifications:\n"
            f"    on_complete: 'echo \"completed:${{name}}\" >> {log_file}'\n"
        )
        engine = Engine(project)

        _, child_id = await _suspended_parent_with_waiting_child(engine)
        await engine.receive_signal(
            child_id,
            "result_ready",
            {"outcome": "pass"},
            target_state="done",
        )
        assert not log_file.exists() or (
            "completed" not in log_file.read_text()
        )

        # Control: standalone instance of the same process, completed via
        # a plain transition after a target-less signal, does notify.
        control_id = engine.start("signal-child")["instance_id"]
        await engine.transition(control_id, "await_result")
        await engine.receive_signal(
            control_id, "result_ready", {"outcome": "pass"}
        )
        await engine.transition(control_id, "done")
        assert log_file.read_text().count("completed:signal-child") == 1

    async def test_event_stream_has_complete_via_signal_but_no_parent_resumed(
        self, engine: Engine, project: Path
    ):
        # Pins current behavior per GH-41: the child stream records the
        # signal-driven completion (complete carries via=signal) while the
        # parent stream never gets a parent_resumed event. FLIP when #41
        # resolves.
        pid, child_id = await _suspended_parent_with_waiting_child(engine)

        await engine.receive_signal(
            child_id,
            "result_ready",
            {"outcome": "pass"},
            target_state="done",
        )

        child_events = _events_for(project, child_id)
        assert _types(child_events) == [
            "started",
            "transition",
            "signal_received",
            "complete",
        ]
        assert child_events[-1]["payload"] == {
            "final_state": "done",
            "via": "signal",
        }

        parent_events = _events_for(project, pid)
        assert _types(parent_events) == [
            "started",
            "transition",
            "subprocess_started",
        ]
        assert "parent_resumed" not in _types(parent_events)


class TestSignalIntoWaitStateDoesNotWait:
    """GH-41 pin: wait-state entry via signal skips the waiting branch."""

    async def _at_first_gate(self, engine: Engine) -> str:
        iid = engine.start("double-wait")["instance_id"]
        await engine.transition(iid, "first_gate")
        return iid

    async def test_signal_into_wait_state_lands_unlocked_not_waiting(
        self, engine: Engine
    ):
        # Pins current behavior per GH-41: a signal targeting a wait state
        # enters it as if it were a normal state; the instance is not
        # marked waiting and its transitions are immediately available.
        # FLIP this test when #41 resolves.
        iid = await self._at_first_gate(engine)

        result = await engine.receive_signal(
            iid, "go", {"note": "onward"}, target_state="second_gate"
        )

        assert result["success"] is True
        assert result["new_state"] == "second_gate"
        assert result["available_transitions"] == ["end"]

        status = engine.status(iid)
        assert status["available_transitions"] == ["end"]
        assert "waiting" not in status
        assert engine._store.load(iid).waiting is False

    async def test_wait_state_entered_via_signal_allows_plain_exit(
        self, engine: Engine
    ):
        # Pins current behavior per GH-41: because the second wait state
        # never set waiting, a plain transition leaves it without any
        # signal being delivered. FLIP when #41 resolves.
        iid = await self._at_first_gate(engine)
        await engine.receive_signal(
            iid, "go", {"note": "onward"}, target_state="second_gate"
        )

        result = await engine.transition(iid, "end")
        assert result.success is True
        assert result.new_state == "end"

    async def test_wait_state_entered_via_signal_rejects_its_own_signal(
        self, engine: Engine
    ):
        # Pins current behavior per GH-41: the second wait state's own
        # signal is refused because the instance was never marked waiting.
        # FLIP when #41 resolves.
        iid = await self._at_first_gate(engine)
        await engine.receive_signal(
            iid, "go", {"note": "onward"}, target_state="second_gate"
        )

        with pytest.raises(TransitionError, match="not waiting"):
            await engine.receive_signal(iid, "confirm", {})


class TestSignalIntoSubprocessStateDoesNotStartChild:
    """GH-41 pin: subprocess-state entry via signal skips child start."""

    async def test_signal_into_subprocess_state_starts_no_child(
        self, engine: Engine
    ):
        # Pins current behavior per GH-41: a signal targeting a
        # subprocess-type state lands there like a normal state; no child
        # instance is created and the instance is not suspended (contrast
        # transition(), which starts the child and suspends; see
        # test_subprocess.py). FLIP this test when #41 resolves.
        iid = engine.start("signal-subprocess-target")["instance_id"]
        await engine.transition(iid, "gate")

        result = await engine.receive_signal(
            iid, "proceed", {}, target_state="delegate"
        )

        assert result["success"] is True
        assert result["new_state"] == "delegate"
        assert "subprocess_started" not in result

        status = engine.status(iid)
        assert status["suspended"] is False
        assert status["current_state"] == "delegate"

        all_instances = engine.status()
        assert [s["instance_id"] for s in all_instances] == [iid]
