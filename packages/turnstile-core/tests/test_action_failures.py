"""Tests for observable on_enter/on_exit action failures (GH #31).

Pins the contract that actions remain non-blocking but never fail silently:
- a non-zero exit emits an ``action_failed`` event and an ACTION FAILED
  log line, while the state change still succeeds
- an execution error (TimeoutError/OSError from run_command) is recorded
  with exit_code None and an error string instead of vanishing
- both the transition and receive_signal paths emit, with session
  attribution
- failures recorded before a blocking gate stay in the stream even when
  the state change is refused (ghost event; consumers dedupe on
  (instance_id, seq))
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

import turnstile_core.engine as engine_module
from turnstile_core.engine import Engine
from turnstile_core.persistence import StateStore

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

PAYLOAD_KEYS = {"phase", "state", "command", "exit_code", "error", "output"}

ENTER_FAIL_PROCESS = {
    "name": "enter-fail",
    "description": "on_enter action exits non-zero",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["work"]},
        {
            "id": "work",
            "on_enter": {"actions": [{"command": "exit 3"}]},
            "transitions": ["done"],
        },
        {"id": "done", "type": "terminal"},
    ],
}

EXIT_FAIL_PROCESS = {
    "name": "exit-fail",
    "description": "on_exit action exits non-zero",
    "version": "1.0.0",
    "states": [
        {
            "id": "start",
            "type": "initial",
            "on_exit": {"actions": [{"command": "exit 4"}]},
            "transitions": ["work"],
        },
        {"id": "work", "transitions": ["done"]},
        {"id": "done", "type": "terminal"},
    ],
}

OK_ACTION_PROCESS = {
    "name": "ok-action",
    "description": "on_enter action succeeds",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["work"]},
        {
            "id": "work",
            "on_enter": {"actions": [{"command": "true"}]},
            "transitions": ["done"],
        },
        {"id": "done", "type": "terminal"},
    ],
}

MULTI_FAIL_PROCESS = {
    "name": "multi-fail",
    "description": "two failing on_enter actions in one hook",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["work"]},
        {
            "id": "work",
            "on_enter": {
                "actions": [{"command": "exit 1"}, {"command": "exit 2"}]
            },
            "transitions": ["done"],
        },
        {"id": "done", "type": "terminal"},
    ],
}

TRUNCATE_PROCESS = {
    "name": "truncate",
    "description": "failing action with more than 500 chars of output",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["work"]},
        {
            "id": "work",
            "on_enter": {
                "actions": [
                    # "HEAD" then 600 zeros, then fail: 604 chars total,
                    # so the stored tail must have dropped the head marker.
                    {"command": "printf HEAD; printf '%0600d' 0; exit 7"}
                ]
            },
            "transitions": ["done"],
        },
        {"id": "done", "type": "terminal"},
    ],
}

GHOST_PROCESS = {
    "name": "ghost",
    "description": "on_exit action fails, then on_enter gate blocks",
    "version": "1.0.0",
    "states": [
        {
            "id": "start",
            "type": "initial",
            "on_exit": {"actions": [{"command": "exit 9"}]},
            "transitions": ["work"],
        },
        {
            "id": "work",
            "on_enter": {
                "validate": [
                    {
                        # blocks until the test creates .gate-open in the
                        # project root, letting a retry succeed
                        "command": "cat .gate-open 2>/dev/null",
                        "expect": "not_empty",
                        "message": "gate file must exist",
                    }
                ]
            },
            "transitions": ["done"],
        },
        {"id": "done", "type": "terminal"},
    ],
}

STDERR_PROCESS = {
    "name": "stderr-diag",
    "description": "failing action whose diagnostics go to stderr",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["work"]},
        {
            "id": "work",
            "on_enter": {"actions": [{"command": "echo diag >&2; exit 4"}]},
            "transitions": ["done"],
        },
        {"id": "done", "type": "terminal"},
    ],
}

NUL_PROCESS = {
    "name": "nul-command",
    "description": "action command with an embedded NUL byte",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["work"]},
        {
            "id": "work",
            # the NUL survives the YAML round-trip ("echo hi\0bad") and
            # makes create_subprocess_shell raise ValueError at spawn
            "on_enter": {"actions": [{"command": "echo hi\x00bad"}]},
            "transitions": ["done"],
        },
        {"id": "done", "type": "terminal"},
    ],
}

LONG_COMMAND = "exit 11 # " + "x" * 600

LONG_COMMAND_PROCESS = {
    "name": "long-command",
    "description": "failing action whose command text exceeds 500 chars",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["work"]},
        {
            "id": "work",
            "on_enter": {"actions": [{"command": LONG_COMMAND}]},
            "transitions": ["done"],
        },
        {"id": "done", "type": "terminal"},
    ],
}

MULTILINE_PROCESS = {
    "name": "multiline",
    "description": "failing action with a multi-line command",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["work"]},
        {
            "id": "work",
            "on_enter": {
                "actions": [{"command": "echo one\necho   two\nexit 8"}]
            },
            "transitions": ["done"],
        },
        {"id": "done", "type": "terminal"},
    ],
}

SIGNAL_EXIT_FAIL_PROCESS = {
    "name": "signal-exit-fail",
    "description": "wait state whose on_exit action fails on signal",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["waiting"]},
        {
            "id": "waiting",
            "type": "wait",
            "signal": {
                "name": "go",
                "required_fields": [{"key": "ok"}],
            },
            "on_exit": {"actions": [{"command": "exit 5"}]},
            "transitions": ["after"],
        },
        {"id": "after", "type": "terminal"},
    ],
}

SIGNAL_ENTER_FAIL_PROCESS = {
    "name": "signal-enter-fail",
    "description": "signal target whose on_enter action fails",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["waiting"]},
        {
            "id": "waiting",
            "type": "wait",
            "signal": {
                "name": "go",
                "required_fields": [{"key": "ok"}],
            },
            "transitions": ["after"],
        },
        {
            "id": "after",
            "type": "terminal",
            "on_enter": {"actions": [{"command": "exit 6"}]},
        },
    ],
}

ALL_PROCESSES = [
    ENTER_FAIL_PROCESS,
    EXIT_FAIL_PROCESS,
    OK_ACTION_PROCESS,
    MULTI_FAIL_PROCESS,
    TRUNCATE_PROCESS,
    GHOST_PROCESS,
    STDERR_PROCESS,
    NUL_PROCESS,
    LONG_COMMAND_PROCESS,
    MULTILINE_PROCESS,
    SIGNAL_EXIT_FAIL_PROCESS,
    SIGNAL_ENTER_FAIL_PROCESS,
]


@pytest.fixture()
def project(tmp_path) -> Path:
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    for process in ALL_PROCESSES:
        (proc_dir / f"{process['name']}.yaml").write_text(yaml.dump(process))
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


def _log_text(project: Path) -> str:
    return (project / ".process-state" / "log.txt").read_text()


def _store(project: Path) -> StateStore:
    return StateStore(project / ".process-state")


# ---------------------------------------------------------------------------
# Transition path: non-zero exit
# ---------------------------------------------------------------------------


class TestTransitionActionFailure:
    async def test_failing_on_enter_action_does_not_block_the_transition(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("enter-fail")["instance_id"]
        result = await engine.transition(iid, "work")

        assert result.success is True
        assert result.new_state == "work"
        instance = _store(project).load(iid)
        assert instance.current_state == "work"
        assert len(instance.history) == 1

    async def test_failing_on_enter_action_emits_action_failed_event(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("enter-fail", session_id="sess-a")["instance_id"]
        await engine.transition(iid, "work", session_id="sess-a")

        events = _events_for(project, iid)
        assert _types(events) == ["started", "action_failed", "transition"]
        event = events[1]
        assert set(event.keys()) == ENVELOPE_KEYS
        assert event["session_id"] == "sess-a"
        assert set(event["payload"].keys()) == PAYLOAD_KEYS
        assert event["payload"] == {
            "phase": "on_enter",
            "state": "work",
            "command": "exit 3",
            "exit_code": 3,
            "error": "",
            "output": "",
        }

    async def test_failing_on_enter_action_appends_action_failed_log_line(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("enter-fail")["instance_id"]
        await engine.transition(iid, "work")

        assert (
            f"ACTION FAILED enter-fail-{iid} on_enter work (exit 3): exit 3"
            in _log_text(project)
        )

    async def test_failing_on_exit_action_records_the_exited_state(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("exit-fail")["instance_id"]
        result = await engine.transition(iid, "work")

        assert result.success is True
        events = _events_for(project, iid)
        assert _types(events) == ["started", "action_failed", "transition"]
        payload = events[1]["payload"]
        assert payload["phase"] == "on_exit"
        assert payload["state"] == "start"
        assert payload["command"] == "exit 4"
        assert payload["exit_code"] == 4
        assert (
            f"ACTION FAILED exit-fail-{iid} on_exit start (exit 4): exit 4"
            in _log_text(project)
        )

    async def test_successful_action_leaves_no_failure_trace(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("ok-action")["instance_id"]
        result = await engine.transition(iid, "work")

        assert result.success is True
        events = _events_for(project, iid)
        assert _types(events) == ["started", "transition"]
        assert "ACTION FAILED" not in _log_text(project)


# ---------------------------------------------------------------------------
# Transition path: execution errors (TimeoutError / OSError)
# ---------------------------------------------------------------------------


class TestActionExecutionError:
    async def test_timeout_is_recorded_without_blocking_the_transition(
        self, engine: Engine, project: Path, monkeypatch
    ):
        async def raise_timeout(*args, **kwargs):
            raise TimeoutError("command timed out")

        monkeypatch.setattr(engine_module, "run_command", raise_timeout)

        iid = engine.start("enter-fail")["instance_id"]
        result = await engine.transition(iid, "work")

        assert result.success is True
        assert result.new_state == "work"
        events = _events_for(project, iid)
        assert _types(events) == ["started", "action_failed", "transition"]
        payload = events[1]["payload"]
        assert payload["exit_code"] is None
        assert payload["error"] == "TimeoutError: command timed out"
        assert payload["output"] == ""
        assert (
            f"ACTION FAILED enter-fail-{iid} on_enter work "
            f"(TimeoutError: command timed out): exit 3"
            in _log_text(project)
        )

    async def test_os_error_is_recorded_without_blocking_the_transition(
        self, engine: Engine, project: Path, monkeypatch
    ):
        async def raise_oserror(*args, **kwargs):
            raise OSError("cannot spawn shell")

        monkeypatch.setattr(engine_module, "run_command", raise_oserror)

        iid = engine.start("enter-fail")["instance_id"]
        result = await engine.transition(iid, "work")

        assert result.success is True
        events = _events_for(project, iid)
        assert _types(events) == ["started", "action_failed", "transition"]
        payload = events[1]["payload"]
        assert payload["exit_code"] is None
        assert payload["error"] == "OSError: cannot spawn shell"


# ---------------------------------------------------------------------------
# Signal path
# ---------------------------------------------------------------------------


class TestSignalActionFailure:
    async def test_failing_wait_state_on_exit_action_is_session_attributed(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("signal-exit-fail")["instance_id"]
        await engine.transition(iid, "waiting")
        result = await engine.receive_signal(
            iid,
            "go",
            {"ok": "yes"},
            target_state="after",
            session_id="sig-sess",
        )

        assert result["success"] is True
        events = _events_for(project, iid)
        assert _types(events) == [
            "started",
            "transition",
            "action_failed",
            "signal_received",
            "complete",
        ]
        event = events[2]
        assert event["session_id"] == "sig-sess"
        payload = event["payload"]
        assert payload["phase"] == "on_exit"
        assert payload["state"] == "waiting"
        assert payload["command"] == "exit 5"
        assert payload["exit_code"] == 5
        assert (
            f"ACTION FAILED signal-exit-fail-{iid} on_exit waiting "
            f"(exit 5): exit 5"
            in _log_text(project)
        )

    async def test_failing_signal_target_on_enter_action_is_recorded(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("signal-enter-fail")["instance_id"]
        await engine.transition(iid, "waiting")
        result = await engine.receive_signal(
            iid,
            "go",
            {"ok": "yes"},
            target_state="after",
            session_id="sig-sess-2",
        )

        assert result["success"] is True
        events = _events_for(project, iid)
        assert _types(events) == [
            "started",
            "transition",
            "action_failed",
            "signal_received",
            "complete",
        ]
        event = events[2]
        assert event["session_id"] == "sig-sess-2"
        payload = event["payload"]
        assert payload["phase"] == "on_enter"
        assert payload["state"] == "after"
        assert payload["exit_code"] == 6


# ---------------------------------------------------------------------------
# Multiple failures, seq discipline
# ---------------------------------------------------------------------------


class TestMultipleFailures:
    async def test_each_failing_action_gets_its_own_event_in_order(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("multi-fail")["instance_id"]
        result = await engine.transition(iid, "work")

        assert result.success is True
        events = _events_for(project, iid)
        assert _types(events) == [
            "started",
            "action_failed",
            "action_failed",
            "transition",
        ]
        assert [e["seq"] for e in events] == [0, 1, 2, 3]
        assert events[1]["payload"]["exit_code"] == 1
        assert events[2]["payload"]["exit_code"] == 2


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------


class TestOutputTruncation:
    async def test_long_output_is_truncated_to_the_last_500_chars(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("truncate")["instance_id"]
        await engine.transition(iid, "work")

        events = _events_for(project, iid)
        assert _types(events) == ["started", "action_failed", "transition"]
        output = events[1]["payload"]["output"]
        # command printed "HEAD" + 600 zeros; the stored tail is exactly
        # the last 500 chars, so the head marker is gone
        assert output == "0" * 500


# ---------------------------------------------------------------------------
# Ghost event: action failure recorded before a blocking gate
# ---------------------------------------------------------------------------


class TestGhostActionFailure:
    async def test_action_failed_survives_a_subsequently_blocked_transition(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("ghost")["instance_id"]
        result = await engine.transition(iid, "work")

        # the on_enter gate refused the state change
        assert result.success is False
        assert result.new_state == "start"
        instance = _store(project).load(iid)
        assert instance.current_state == "start"
        assert instance.history == []

        # ...but the on_exit action failure was already appended and stays
        events = _events_for(project, iid)
        assert _types(events) == ["started", "action_failed"]
        payload = events[1]["payload"]
        assert payload["phase"] == "on_exit"
        assert payload["state"] == "start"
        assert payload["exit_code"] == 9

        # contract: even though the transition never landed, the advanced
        # seq counter IS persisted, so no later operation can re-emit at
        # seq 1 and shadow the failure record under dedupe-keep-last
        assert events[1]["seq"] == 1
        assert instance.event_seq == 2

    async def test_retry_after_blocked_transition_does_not_collide_with_seq(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("ghost")["instance_id"]
        blocked = await engine.transition(iid, "work")
        assert blocked.success is False

        # open the gate through the filesystem, then retry
        (project / ".gate-open").write_text("open\n")
        retried = await engine.transition(iid, "work")

        assert retried.success is True
        assert retried.new_state == "work"
        events = _events_for(project, iid)
        # the retry re-runs (and re-fails) the on_exit action, then lands
        assert _types(events) == [
            "started",
            "action_failed",  # blocked attempt
            "action_failed",  # retry's own on_exit failure
            "transition",
        ]
        # gapless and collision-free: the retry starts at seq 2, leaving
        # the blocked attempt's failure record at seq 1 intact
        assert [e["seq"] for e in events] == [0, 1, 2, 3]
        instance = _store(project).load(iid)
        assert instance.current_state == "work"
        assert len(instance.history) == 1
        assert instance.event_seq == 4


# ---------------------------------------------------------------------------
# stderr capture, ValueError spawn rejects, payload/log hygiene
# ---------------------------------------------------------------------------


class TestFailureDiagnostics:
    async def test_stderr_diagnostics_are_captured_in_the_failure_output(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("stderr-diag")["instance_id"]
        result = await engine.transition(iid, "work")

        assert result.success is True
        events = _events_for(project, iid)
        assert _types(events) == ["started", "action_failed", "transition"]
        payload = events[1]["payload"]
        assert payload["exit_code"] == 4
        assert payload["output"] == "diag"

    async def test_embedded_nul_in_command_does_not_abort_the_transition(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("nul-command")["instance_id"]
        result = await engine.transition(iid, "work")

        assert result.success is True
        assert result.new_state == "work"
        events = _events_for(project, iid)
        assert _types(events) == ["started", "action_failed", "transition"]
        payload = events[1]["payload"]
        assert payload["exit_code"] is None
        assert payload["error"].startswith("ValueError")

    async def test_command_longer_than_500_chars_is_stored_head_truncated(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("long-command")["instance_id"]
        await engine.transition(iid, "work")

        events = _events_for(project, iid)
        assert _types(events) == ["started", "action_failed", "transition"]
        assert events[1]["payload"]["command"] == LONG_COMMAND[:500]

    async def test_multiline_command_produces_a_single_collapsed_log_line(
        self, engine: Engine, project: Path
    ):
        iid = engine.start("multiline")["instance_id"]
        await engine.transition(iid, "work")

        log_lines = [
            line for line in _log_text(project).splitlines()
            if "ACTION FAILED" in line
        ]
        assert len(log_lines) == 1
        assert log_lines[0].endswith(
            f"ACTION FAILED multiline-{iid} on_enter work "
            f"(exit 8): echo one echo two exit 8"
        )

    async def test_unwritable_log_does_not_abort_the_transition(
        self, engine: Engine, project: Path, monkeypatch
    ):
        original = engine._store.append_log

        def flaky_append(message: str) -> None:
            if message.startswith("ACTION FAILED"):
                raise OSError("read-only filesystem")
            original(message)

        monkeypatch.setattr(engine._store, "append_log", flaky_append)

        iid = engine.start("enter-fail")["instance_id"]
        result = await engine.transition(iid, "work")

        assert result.success is True
        assert result.new_state == "work"
        # the event still made it to the stream even though the log write
        # was lost
        events = _events_for(project, iid)
        assert _types(events) == ["started", "action_failed", "transition"]
        assert "ACTION FAILED" not in _log_text(project)
