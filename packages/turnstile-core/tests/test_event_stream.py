"""Tests for the shadow event stream (GH #29, events.jsonl).

Pins the stream contract ahead of the event-sourced flip:
- envelope shape and per-instance gapless sequence numbers
- the exact event set each engine operation emits
- append-only discipline
- fold(events) sufficiency: replaying one instance's events reconstructs
  the persisted instance's projection (current_state, history length,
  overrides length, status)
- fail-open emission: a broken stream never breaks live operations
- legacy state files without event_seq still load
"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from turnstile_core.engine import Engine
from turnstile_core.persistence import StateStore

FIXTURES = Path(__file__).parent / "fixtures"

ENVELOPE_KEYS = {
    "event_type",
    "instance_id",
    "process_name",
    "seq",
    "at",
    "session_id",
    "actor",
    "payload",
}

WAIT_PROCESS = {
    "name": "wait-test",
    "description": "Process with a wait state",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["work"]},
        {"id": "work", "description": "Do work", "transitions": ["review"]},
        {
            "id": "review",
            "type": "wait",
            "role": "reviewer",
            "signal": {
                "name": "review_complete",
                "required_fields": [{"key": "approved"}],
            },
            "transitions": ["approved", "rejected"],
        },
        {"id": "approved", "type": "terminal"},
        {"id": "rejected", "transitions": ["work"]},
    ],
}

CHILD_PROCESS = {
    "name": "child-task",
    "description": "A simple child task",
    "version": "1.0.0",
    "parameters": [
        {"name": "task_name", "description": "What to do", "required": True},
    ],
    "states": [
        {"id": "start", "type": "initial", "transitions": ["work"]},
        {"id": "work", "description": "Do the work", "transitions": ["done"]},
        {"id": "done", "type": "terminal"},
    ],
}

COORDINATOR_PROCESS = {
    "name": "coordinator",
    "description": "Coordinator that dispatches work",
    "version": "1.0.0",
    "parameters": [
        {"name": "task_name", "description": "Task to dispatch"},
    ],
    "states": [
        {"id": "start", "type": "initial", "transitions": ["dispatch_work"]},
        {
            "id": "dispatch_work",
            "type": "dispatch",
            "process": "child-task",
            "parameter_map": {"task_name": "${task_name}"},
            "immediate": "ready",
            "assign_to": "developer",
        },
        {"id": "ready", "transitions": ["shutdown"]},
        {"id": "shutdown", "type": "terminal"},
    ],
}


SIGNAL_DISPATCH_PROCESS = {
    "name": "signal-dispatch-test",
    "description": "Wait state whose signal target is a dispatch state",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["waitq"]},
        {
            "id": "waitq",
            "type": "wait",
            "signal": {
                "name": "job_ready",
                "required_fields": [{"key": "task_name"}],
            },
            "transitions": ["run_job"],
        },
        {
            "id": "run_job",
            "type": "dispatch",
            "process": "child-task",
            "parameter_map": {"task_name": "${task_name}"},
            "immediate": "ready",
        },
        {"id": "ready", "transitions": ["shutdown"]},
        {"id": "shutdown", "type": "terminal"},
    ],
}


@pytest.fixture()
def project(tmp_path) -> Path:
    """Project with every process the stream tests exercise."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
    shutil.copy(
        FIXTURES / "subprocess-parent.yaml", proc_dir / "subprocess-parent.yaml"
    )
    shutil.copy(
        FIXTURES / "subprocess-child.yaml", proc_dir / "subprocess-child.yaml"
    )
    (proc_dir / "wait-test.yaml").write_text(yaml.dump(WAIT_PROCESS))
    (proc_dir / "coordinator.yaml").write_text(yaml.dump(COORDINATOR_PROCESS))
    (proc_dir / "child-task.yaml").write_text(yaml.dump(CHILD_PROCESS))
    (proc_dir / "signal-dispatch-test.yaml").write_text(
        yaml.dump(SIGNAL_DISPATCH_PROCESS)
    )
    return tmp_path


@pytest.fixture()
def engine(project: Path) -> Engine:
    return Engine(project)


def _events(project: Path) -> list[dict[str, Any]]:
    path = project / ".process-state" / "events.jsonl"
    assert path.exists(), "events.jsonl was not created"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _events_for(project: Path, instance_id: str) -> list[dict[str, Any]]:
    return [e for e in _events(project) if e["instance_id"] == instance_id]


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [e["event_type"] for e in events]


def _store(project: Path) -> StateStore:
    return StateStore(project / ".process-state")


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class TestEnvelope:
    async def test_every_line_is_json_with_the_documented_envelope(
        self, engine: Engine, project: Path
    ):
        started = engine.start("simple", {"task_name": "t"})
        iid = started["instance_id"]
        await engine.transition(iid, "working")

        events = _events(project)
        assert events, "no events were written"
        for event in events:
            assert set(event.keys()) == ENVELOPE_KEYS
            assert isinstance(event["seq"], int)
            assert isinstance(event["payload"], dict)
            assert isinstance(event["session_id"], str)
            assert isinstance(event["actor"], str)
            # at must be a parseable ISO-8601 timestamp
            datetime.fromisoformat(event["at"])

    async def test_seq_starts_at_zero_and_has_no_gaps(
        self, engine: Engine, project: Path
    ):
        started = engine.start("simple", {"task_name": "t"})
        iid = started["instance_id"]
        await engine.transition(iid, "working")
        await engine.transition(iid, "review")
        await engine.transition(iid, "done")

        seqs = [e["seq"] for e in _events_for(project, iid)]
        assert seqs == list(range(len(seqs)))
        assert len(seqs) >= 4  # started + 3 transitions (+ complete)

    async def test_seq_is_per_instance_not_global(
        self, engine: Engine, project: Path
    ):
        a = engine.start("simple", {"task_name": "a"})["instance_id"]
        b = engine.start("simple", {"task_name": "b"})["instance_id"]
        await engine.transition(a, "working")
        await engine.transition(b, "working")

        seqs_a = [e["seq"] for e in _events_for(project, a)]
        seqs_b = [e["seq"] for e in _events_for(project, b)]
        assert seqs_a == [0, 1]
        assert seqs_b == [0, 1]


# ---------------------------------------------------------------------------
# Event sets per operation
# ---------------------------------------------------------------------------


class TestStartEvents:
    def test_start_emits_single_started_event(
        self, engine: Engine, project: Path
    ):
        started = engine.start("simple", {"task_name": "build"})
        iid = started["instance_id"]

        events = _events_for(project, iid)
        assert _types(events) == ["started"]
        payload = events[0]["payload"]
        assert payload["initial_state"] == "start"
        assert payload["parameters"] == {"task_name": "build"}
        assert events[0]["process_name"] == "simple"

    def test_started_event_carries_session_id(
        self, engine: Engine, project: Path
    ):
        started = engine.start(
            "simple", {"task_name": "t"}, session_id="sess-x"
        )
        iid = started["instance_id"]

        events = _events_for(project, iid)
        assert _types(events) == ["started"]
        assert events[0]["session_id"] == "sess-x"


class TestTransitionEvents:
    async def test_normal_transition_emits_transition_matching_history(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "working", session_id="sess-1")
        await engine.transition(
            iid, "review", metadata={"note": "checked"}, session_id="sess-1"
        )

        events = _events_for(project, iid)
        assert _types(events) == ["started", "transition", "transition"]

        review_event = events[2]
        assert review_event["session_id"] == "sess-1"
        payload = review_event["payload"]
        # payload mirrors the persisted history entry field-for-field
        instance = _store(project).load(iid)
        entry = instance.history[1]
        assert payload["from"] == entry.from_state == "working"
        assert payload["to"] == entry.to_state == "review"
        assert payload["role"] == entry.role
        assert payload["triggered_by"] == entry.triggered_by
        assert payload["validations"] == entry.validations
        assert payload["metadata"] == entry.metadata == {"note": "checked"}
        # working->review runs one exit gate and one enter gate
        assert len(payload["validations"]) == 2

    async def test_transition_to_terminal_appends_complete(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "working")
        await engine.transition(iid, "review")
        await engine.transition(iid, "done")

        events = _events_for(project, iid)
        assert _types(events) == [
            "started",
            "transition",
            "transition",
            "transition",
            "complete",
        ]
        assert events[-1]["payload"] == {"final_state": "done"}


class TestSkipEvents:
    async def test_skip_emits_skip_with_reason(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.skip(iid, "review", "urgent bypass", session_id="s-9")

        events = _events_for(project, iid)
        assert _types(events) == ["started", "skip"]
        payload = events[1]["payload"]
        assert payload["reason"] == "urgent bypass"
        assert payload["from"] == "start"
        assert payload["to"] == "review"
        assert events[1]["session_id"] == "s-9"

    async def test_skip_to_terminal_appends_complete_via_skip(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.skip(iid, "done", "abort straight to done")

        events = _events_for(project, iid)
        assert _types(events) == ["started", "skip", "complete"]
        assert events[2]["payload"] == {"final_state": "done", "via": "skip"}


class TestAbandonEvents:
    def test_abandon_emits_abandon_with_final_state_and_reason(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        engine.abandon(iid, "no longer needed")

        events = _events_for(project, iid)
        assert _types(events) == ["started", "abandon"]
        assert events[1]["payload"] == {
            "final_state": "start",
            "reason": "no longer needed",
        }


class TestUndoEvents:
    async def test_undo_emits_undo_with_restored_state(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "working")
        await engine.transition(iid, "review")
        engine.undo(iid, "wrong branch", session_id="s-undo")

        events = _events_for(project, iid)
        assert _types(events) == [
            "started",
            "transition",
            "transition",
            "undo",
        ]
        assert events[3]["payload"] == {
            "removed_state": "review",
            "restored_state": "working",
            "reason": "wrong branch",
        }
        assert events[3]["session_id"] == "s-undo"


class TestHandoffEvents:
    def test_handoff_emits_handoff_with_new_owner_as_actor(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        engine.handoff(iid, "bob", "vacation coverage", session_id="sess-h")

        events = _events_for(project, iid)
        assert _types(events) == ["started", "handoff"]
        assert events[1]["actor"] == "bob"
        assert events[1]["session_id"] == "sess-h"
        assert events[1]["payload"] == {
            "from_user": "",
            "to_user": "bob",
            "reason": "vacation coverage",
        }


class TestWaitAndSignalEvents:
    async def _to_wait(self, engine: Engine) -> str:
        iid = engine.start("wait-test")["instance_id"]
        await engine.transition(iid, "work")
        await engine.transition(iid, "review")
        return iid

    async def test_entering_wait_state_marks_transition_event_waiting(
        self, engine: Engine, project: Path
    ):
        iid = await self._to_wait(engine)

        events = _events_for(project, iid)
        assert _types(events) == ["started", "transition", "transition"]
        payload = events[2]["payload"]
        assert payload["waiting"] is True
        assert payload["signal"] == "review_complete"
        assert payload["to"] == "review"

    async def test_signal_without_target_records_awaiting_transition(
        self, engine: Engine, project: Path
    ):
        iid = await self._to_wait(engine)
        await engine.receive_signal(
            iid, "review_complete", {"approved": "yes"}
        )

        events = _events_for(project, iid)
        assert _types(events)[-1] == "signal_received"
        payload = events[-1]["payload"]
        assert payload["awaiting_transition"] is True
        assert payload["signal"] == "review_complete"
        assert payload["data"] == {"approved": "yes"}

    async def test_signal_with_target_carries_entry_fields_and_data(
        self, engine: Engine, project: Path
    ):
        iid = await self._to_wait(engine)
        await engine.receive_signal(
            iid,
            "review_complete",
            {"approved": "no"},
            target_state="rejected",
            session_id="sig-sess",
        )

        events = _events_for(project, iid)
        assert _types(events) == [
            "started",
            "transition",
            "transition",
            "signal_received",
        ]
        event = events[3]
        assert event["session_id"] == "sig-sess"
        payload = event["payload"]
        assert payload["from"] == "review"
        assert payload["to"] == "rejected"
        assert payload["signal"] == "review_complete"
        assert payload["data"] == {"approved": "no"}
        assert payload["triggered_by"] == "signal: review_complete"
        assert payload["metadata"] == {"signal_data": {"approved": "no"}}

    async def test_signal_to_terminal_appends_complete_via_signal(
        self, engine: Engine, project: Path
    ):
        iid = await self._to_wait(engine)
        await engine.receive_signal(
            iid,
            "review_complete",
            {"approved": "yes"},
            target_state="approved",
        )

        events = _events_for(project, iid)
        assert _types(events) == [
            "started",
            "transition",
            "transition",
            "signal_received",
            "complete",
        ]
        assert events[-1]["payload"] == {
            "final_state": "approved",
            "via": "signal",
        }


class TestDispatchEvents:
    async def test_dispatch_emits_transition_then_dispatch_on_parent(
        self, engine: Engine, project: Path
    ):
        pid = engine.start("coordinator", {"task_name": "build"})[
            "instance_id"
        ]
        result = await engine.transition(pid, "dispatch_work")
        child_id = result.subprocess_started

        events = _events_for(project, pid)
        assert _types(events) == ["started", "transition", "dispatch"]
        transition_payload = events[1]["payload"]
        assert transition_payload["from"] == "start"
        assert transition_payload["to"] == "dispatch_work"
        dispatch_payload = events[2]["payload"]
        assert dispatch_payload["from"] == "dispatch_work"
        assert dispatch_payload["to"] == "ready"
        assert dispatch_payload["child_instance_id"] == child_id
        assert dispatch_payload["child_process"] == "child-task"

    async def test_dispatched_child_gets_its_own_started_event(
        self, engine: Engine, project: Path
    ):
        pid = engine.start("coordinator", {"task_name": "build"})[
            "instance_id"
        ]
        result = await engine.transition(pid, "dispatch_work")
        child_id = result.subprocess_started

        child_events = _events_for(project, child_id)
        assert _types(child_events) == ["started"]
        assert child_events[0]["seq"] == 0
        payload = child_events[0]["payload"]
        assert payload["parent_instance_id"] == pid
        assert payload["parameters"] == {"task_name": "build"}
        assert payload["assigned_to"] == "developer"


class TestSignalIntoDispatchEvents:
    async def test_signal_into_dispatch_emits_signal_then_dispatch(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("signal-dispatch-test")["instance_id"]
        await engine.transition(iid, "waitq")
        result = await engine.receive_signal(
            iid,
            "job_ready",
            {"task_name": "build"},
            target_state="run_job",
        )
        child_id = result["subprocess_started"]

        events = _events_for(project, iid)
        assert _types(events) == [
            "started",
            "transition",
            "signal_received",
            "dispatch",
        ]
        sig_payload = events[2]["payload"]
        assert sig_payload["signal"] == "job_ready"
        assert sig_payload["data"] == {"task_name": "build"}
        assert sig_payload["from"] == "waitq"
        assert sig_payload["to"] == "run_job"
        dispatch_payload = events[3]["payload"]
        assert dispatch_payload["from"] == "run_job"
        assert dispatch_payload["to"] == "ready"
        assert dispatch_payload["child_instance_id"] == child_id
        assert dispatch_payload["child_process"] == "child-task"

    async def test_signal_dispatched_child_gets_its_own_started_event(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("signal-dispatch-test")["instance_id"]
        await engine.transition(iid, "waitq")
        result = await engine.receive_signal(
            iid,
            "job_ready",
            {"task_name": "build"},
            target_state="run_job",
        )
        child_id = result["subprocess_started"]

        child_events = _events_for(project, child_id)
        assert _types(child_events) == ["started"]
        assert child_events[0]["seq"] == 0
        payload = child_events[0]["payload"]
        assert payload["parent_instance_id"] == iid
        assert payload["parameters"] == {"task_name": "build"}


class TestSubprocessEvents:
    async def test_subprocess_lifecycle_events_on_parent_and_child(
        self, engine: Engine, project: Path
    ):
        pid = engine.start("deploy-pipeline", {"deploy_target": "prod"})[
            "instance_id"
        ]
        await engine.transition(pid, "build")
        result = await engine.transition(pid, "test")
        child_id = result.subprocess_started

        parent_events = _events_for(project, pid)
        assert _types(parent_events) == [
            "started",
            "transition",
            "transition",
            "subprocess_started",
        ]
        sub_payload = parent_events[3]["payload"]
        assert sub_payload["child_instance_id"] == child_id
        assert sub_payload["child_process"] == "integration-tests"
        assert sub_payload["suspended"] is True

        child_events = _events_for(project, child_id)
        assert _types(child_events) == ["started"]
        assert child_events[0]["payload"]["parent_instance_id"] == pid

    async def test_child_completion_emits_parent_resumed(
        self, engine: Engine, project: Path
    ):
        pid = engine.start("deploy-pipeline", {"deploy_target": "prod"})[
            "instance_id"
        ]
        await engine.transition(pid, "build")
        result = await engine.transition(pid, "test")
        child_id = result.subprocess_started

        await engine.transition(child_id, "run")
        await engine.transition(child_id, "done")

        child_events = _events_for(project, child_id)
        assert _types(child_events) == [
            "started",
            "transition",
            "transition",
            "complete",
        ]

        parent_events = _events_for(project, pid)
        assert _types(parent_events) == [
            "started",
            "transition",
            "transition",
            "subprocess_started",
            "parent_resumed",
        ]
        resumed_payload = parent_events[4]["payload"]
        assert resumed_payload["child_instance_id"] == child_id
        assert resumed_payload["outcome"] == "completed"
        # parent seq stays gapless across suspension and resume
        assert [e["seq"] for e in parent_events] == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


class TestAppendOnly:
    async def test_earlier_bytes_are_never_rewritten(
        self, engine: Engine, project: Path
    ):
        events_path = project / ".process-state" / "events.jsonl"
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "working")

        prefix = events_path.read_bytes()

        await engine.transition(iid, "review")
        engine.undo(iid, "back up")
        await engine.skip(iid, "done", "finish")
        engine.start("simple", {"task_name": "another"})

        final = events_path.read_bytes()
        assert final.startswith(prefix)
        assert len(final) > len(prefix)


# ---------------------------------------------------------------------------
# Fold acceptance: the stream must be sufficient to rebuild the projection
# ---------------------------------------------------------------------------


def fold(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Replay one instance's events into the persisted-instance projection.

    Reconstructs current_state, history length, overrides length, and
    status. Raises on any event type it does not know how to apply, so a
    new emission the fold cannot absorb fails loudly instead of silently
    passing.
    """
    state: dict[str, Any] = {
        "current_state": None,
        "history": 0,
        "overrides": 0,
        "status": None,
    }
    for event in events:
        etype = event["event_type"]
        payload = event["payload"]
        if etype == "started":
            state["current_state"] = payload["initial_state"]
            state["status"] = "active"
        elif etype in ("transition", "dispatch"):
            state["current_state"] = payload["to"]
            state["history"] += 1
        elif etype == "skip":
            state["current_state"] = payload["to"]
            state["history"] += 1
            state["overrides"] += 1
        elif etype == "undo":
            state["current_state"] = payload["restored_state"]
            state["history"] -= 1
        elif etype == "signal_received":
            if not payload.get("awaiting_transition"):
                state["current_state"] = payload["to"]
                state["history"] += 1
        elif etype == "complete":
            state["status"] = "completed"
        elif etype == "abandon":
            state["status"] = "abandoned"
        elif etype in ("subprocess_started", "parent_resumed", "handoff"):
            pass  # no effect on the four projected fields
        else:
            raise AssertionError(
                f"fold cannot apply unknown event type '{etype}'"
            )
    return state


class TestFoldReconstruction:
    async def test_fold_rebuilds_multi_operation_instance(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "working")
        await engine.transition(iid, "review")
        engine.undo(iid, "not ready for review")
        await engine.skip(iid, "done", "cutting losses")

        folded = fold(_events_for(project, iid))
        instance = _store(project).load_any(iid)

        assert folded["current_state"] == instance.current_state == "done"
        assert folded["history"] == len(instance.history)
        assert folded["overrides"] == len(instance.overrides)
        assert folded["status"] == instance.status == "completed"

    async def test_fold_rebuilds_signal_transition_instance(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("wait-test")["instance_id"]
        await engine.transition(iid, "work")
        await engine.transition(iid, "review")
        await engine.receive_signal(
            iid,
            "review_complete",
            {"approved": "no"},
            target_state="rejected",
        )

        folded = fold(_events_for(project, iid))
        instance = _store(project).load(iid)

        assert folded["current_state"] == instance.current_state == "rejected"
        assert folded["history"] == len(instance.history)
        assert folded["overrides"] == len(instance.overrides)
        assert folded["status"] == instance.status == "active"

    async def test_fold_rebuilds_dispatch_instance(
        self, engine: Engine, project: Path
    ):
        pid = engine.start("coordinator", {"task_name": "build"})[
            "instance_id"
        ]
        await engine.transition(pid, "dispatch_work")

        folded = fold(_events_for(project, pid))
        instance = _store(project).load(pid)

        assert folded["current_state"] == instance.current_state == "ready"
        # manual transition plus the automatic dispatch continuation
        assert folded["history"] == len(instance.history) == 2
        assert folded["overrides"] == len(instance.overrides)
        assert folded["status"] == instance.status == "active"


# ---------------------------------------------------------------------------
# Emission robustness
# ---------------------------------------------------------------------------


class TestEmissionRobustness:
    async def test_non_serializable_metadata_still_emits_the_event(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        stamp = datetime(2026, 7, 23, 12, 30, 0)
        await engine.transition(iid, "working", metadata={"when": stamp})

        events = _events_for(project, iid)
        assert _types(events) == ["started", "transition"]
        assert [e["seq"] for e in events] == [0, 1]
        # the value survives in string form instead of the event being
        # silently dropped while history keeps the entry
        assert events[1]["payload"]["metadata"]["when"] == str(stamp)

    async def test_failed_append_reuses_its_seq_leaving_no_gap(
        self, engine: Engine, project: Path, monkeypatch
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]

        original = StateStore.append_event

        def boom(self, event):
            raise OSError("disk full")

        monkeypatch.setattr(StateStore, "append_event", boom)
        await engine.transition(iid, "working")  # emission lost, fail-open
        monkeypatch.setattr(StateStore, "append_event", original)
        await engine.transition(iid, "review")

        events = _events_for(project, iid)
        assert _types(events) == ["started", "transition"]
        assert [e["seq"] for e in events] == [0, 1]
        # the reused seq-1 slot belongs to the later operation
        assert events[1]["payload"]["to"] == "review"
        # persisted counter agrees with the file
        instance = _store(project).load(iid)
        assert instance.event_seq == len(events)


# ---------------------------------------------------------------------------
# Fail-open emission
# ---------------------------------------------------------------------------


class TestFailOpenEmission:
    def test_start_survives_broken_event_stream(
        self, engine: Engine, project: Path, monkeypatch
    ):
        def boom(self, event):
            raise OSError("disk full")

        monkeypatch.setattr(StateStore, "append_event", boom)

        started = engine.start("simple", {"task_name": "t"})
        iid = started["instance_id"]
        assert started["current_state"] == "start"

        # instance persisted despite the stream failure
        instance = _store(project).load(iid)
        assert instance.current_state == "start"

    async def test_transition_survives_broken_event_stream(
        self, engine: Engine, project: Path, monkeypatch
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]

        def boom(self, event):
            raise OSError("disk full")

        monkeypatch.setattr(StateStore, "append_event", boom)

        result = await engine.transition(iid, "working")
        assert result.success is True
        assert result.new_state == "working"

        instance = _store(project).load(iid)
        assert instance.current_state == "working"
        assert len(instance.history) == 1


# ---------------------------------------------------------------------------
# Legacy state files
# ---------------------------------------------------------------------------


class TestLegacyStateFiles:
    def test_state_file_without_event_seq_loads_with_default_zero(
        self, tmp_path
    ):
        store = StateStore(tmp_path / ".process-state")
        instance = store.create("simple", "start")

        path = store.active_dir / f"simple-{instance.instance_id}.json"
        data = json.loads(path.read_text())
        del data["event_seq"]
        path.write_text(json.dumps(data))

        loaded = store.load(instance.instance_id)
        assert loaded.event_seq == 0
