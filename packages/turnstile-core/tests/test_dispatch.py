"""Tests for async dispatch (fire-and-forget subprocess)."""

from pathlib import Path

import pytest
import yaml

from turnstile_core.engine import Engine
from turnstile_core.errors import SubprocessError, TransitionError
from turnstile_core.models import ProcessState, StateType


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestDispatchModels:
    def test_dispatch_requires_process(self):
        with pytest.raises(ValueError, match="must have 'process'"):
            ProcessState(
                id="test",
                type=StateType.dispatch,
                immediate="ready",
            )

    def test_dispatch_requires_immediate(self):
        with pytest.raises(ValueError, match="must have 'immediate'"):
            ProcessState(
                id="test",
                type=StateType.dispatch,
                process="child",
            )

    def test_dispatch_no_transitions(self):
        with pytest.raises(ValueError, match="must not have 'transitions'"):
            ProcessState(
                id="test",
                type=StateType.dispatch,
                process="child",
                immediate="ready",
                transitions=["ready"],
            )

    def test_normal_state_no_immediate(self):
        with pytest.raises(ValueError, match="must not have 'immediate'"):
            ProcessState(
                id="test",
                type=StateType.normal,
                immediate="ready",
            )

    def test_valid_dispatch_state(self):
        state = ProcessState(
            id="dispatch_work",
            type=StateType.dispatch,
            process="child-process",
            immediate="ready",
            assign_to="developer",
            parameter_map={"task": "${task_name}"},
        )
        assert state.process == "child-process"
        assert state.immediate == "ready"
        assert state.assign_to == "developer"


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


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

PARENT_PROCESS = {
    "name": "coordinator",
    "description": "Coordinator that dispatches work",
    "version": "1.0.0",
    "parameters": [
        {"name": "task_name", "description": "Task to dispatch"},
    ],
    "states": [
        {"id": "start", "type": "initial", "transitions": ["triage"]},
        {"id": "triage", "description": "Decide what to do", "transitions": ["dispatch_work"]},
        {
            "id": "dispatch_work",
            "type": "dispatch",
            "process": "child-task",
            "parameter_map": {"task_name": "${task_name}"},
            "immediate": "ready",
            "assign_to": "developer",
        },
        {
            "id": "ready",
            "description": "Ready for more work",
            "transitions": ["triage", "shutdown"],
        },
        {"id": "shutdown", "type": "terminal"},
    ],
}


@pytest.fixture
def engine(tmp_path):
    """Create an engine with coordinator and child-task processes."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "coordinator.yaml").write_text(yaml.dump(PARENT_PROCESS))
    (proc_dir / "child-task.yaml").write_text(yaml.dump(CHILD_PROCESS))
    return Engine(tmp_path)


class TestDispatchTransition:
    def test_dispatch_creates_child_and_continues(self, engine):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = engine.start("coordinator", {"task_name": "build feature"})
            pid = result["instance_id"]

            loop.run_until_complete(engine.transition(pid, "triage"))
            r = loop.run_until_complete(engine.transition(pid, "dispatch_work"))
        finally:
            loop.close()

        # Parent should be at 'ready', not 'dispatch_work'
        assert r.success
        assert r.new_state == "ready"
        assert "triage" in r.available_transitions
        assert r.subprocess_started is not None
        assert "Dispatched" in r.message

    def test_parent_not_suspended(self, engine):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = engine.start("coordinator", {"task_name": "build feature"})
            pid = result["instance_id"]

            loop.run_until_complete(engine.transition(pid, "triage"))
            r = loop.run_until_complete(engine.transition(pid, "dispatch_work"))

            # Parent should not be suspended
            instance = engine._store.load(pid)
            assert not instance.suspended
            assert instance.current_state == "ready"

            # Parent can transition normally
            r2 = loop.run_until_complete(engine.transition(pid, "triage"))
            assert r2.success
        finally:
            loop.close()

    def test_child_has_parent_link(self, engine):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = engine.start("coordinator", {"task_name": "build feature"})
            pid = result["instance_id"]

            loop.run_until_complete(engine.transition(pid, "triage"))
            r = loop.run_until_complete(engine.transition(pid, "dispatch_work"))
            child_id = r.subprocess_started

            child = engine._store.load(child_id)
            assert child.parent_instance_id == pid
            assert child.process_name == "child-task"
            assert child.parameters["task_name"] == "build feature"
            assert child.started_by == "developer"
        finally:
            loop.close()

    def test_child_runs_independently(self, engine):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = engine.start("coordinator", {"task_name": "build feature"})
            pid = result["instance_id"]

            loop.run_until_complete(engine.transition(pid, "triage"))
            r = loop.run_until_complete(engine.transition(pid, "dispatch_work"))
            child_id = r.subprocess_started

            # Work on the child independently
            loop.run_until_complete(engine.transition(child_id, "work"))
            r_child = loop.run_until_complete(engine.transition(child_id, "done"))
            assert r_child.success
            assert r_child.new_state == "done"

            # Parent should still be at ready, unaffected
            parent = engine._store.load(pid)
            assert parent.current_state == "ready"
            assert not parent.suspended
        finally:
            loop.close()

    def test_dispatch_history_records_metadata(self, engine):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = engine.start("coordinator", {"task_name": "build feature"})
            pid = result["instance_id"]

            loop.run_until_complete(engine.transition(pid, "triage"))
            loop.run_until_complete(
                engine.transition(pid, "dispatch_work", session_id="test-session")
            )

            instance = engine._store.load(pid)
            # Should have: start->triage, triage->dispatch_work, dispatch_work->ready
            assert len(instance.history) == 3
            dispatch_entry = instance.history[2]
            assert dispatch_entry.from_state == "dispatch_work"
            assert dispatch_entry.to_state == "ready"
            assert dispatch_entry.triggered_by == "dispatch: child-task"
            assert "dispatched_instance" in dispatch_entry.metadata
            assert dispatch_entry.session_id == "test-session"
        finally:
            loop.close()

    def test_multiple_dispatches(self, engine):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = engine.start("coordinator", {"task_name": "task A"})
            pid = result["instance_id"]

            # First dispatch
            loop.run_until_complete(engine.transition(pid, "triage"))
            r1 = loop.run_until_complete(engine.transition(pid, "dispatch_work"))
            child1 = r1.subprocess_started
            assert r1.new_state == "ready"

            # Second dispatch (back to triage, then dispatch again)
            loop.run_until_complete(engine.transition(pid, "triage"))
            r2 = loop.run_until_complete(engine.transition(pid, "dispatch_work"))
            child2 = r2.subprocess_started
            assert r2.new_state == "ready"

            # Two different children
            assert child1 != child2

            # Both children are active
            c1 = engine._store.load(child1)
            c2 = engine._store.load(child2)
            assert c1.status == "active"
            assert c2.status == "active"
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Parameter forwarding from transition metadata
# ---------------------------------------------------------------------------


DYNAMIC_PARENT = {
    "name": "dynamic-coordinator",
    "description": "Coordinator that forwards metadata as child parameters",
    "version": "1.0.0",
    "parameters": [
        {"name": "project_name", "description": "Project name"},
    ],
    "states": [
        {"id": "start", "type": "initial", "transitions": ["triage"]},
        {"id": "triage", "transitions": ["dispatch_work"]},
        {
            "id": "dispatch_work",
            "type": "dispatch",
            "process": "child-task",
            "parameter_map": {"task_name": "${task_name}"},
            "immediate": "ready",
        },
        {"id": "ready", "transitions": ["triage", "shutdown"]},
        {"id": "shutdown", "type": "terminal"},
    ],
}


@pytest.fixture
def dynamic_engine(tmp_path):
    """Engine where parent does NOT declare task_name as a parameter."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "dynamic-coordinator.yaml").write_text(yaml.dump(DYNAMIC_PARENT))
    (proc_dir / "child-task.yaml").write_text(yaml.dump(CHILD_PROCESS))
    return Engine(tmp_path)


class TestParameterForwarding:
    def test_metadata_provides_child_params(self, dynamic_engine):
        """Parent doesn't have task_name, but metadata does."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = dynamic_engine.start(
                "dynamic-coordinator", {"project_name": "test-project"}
            )
            pid = result["instance_id"]

            loop.run_until_complete(dynamic_engine.transition(pid, "triage"))
            r = loop.run_until_complete(
                dynamic_engine.transition(
                    pid, "dispatch_work",
                    metadata={"task_name": "implement auth"},
                )
            )

            assert r.success
            child_id = r.subprocess_started
            child = dynamic_engine._store.load(child_id)
            assert child.parameters["task_name"] == "implement auth"
        finally:
            loop.close()

    def test_metadata_overrides_parent_params(self, engine):
        """Metadata values take precedence over parent parameters."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = engine.start("coordinator", {"task_name": "original"})
            pid = result["instance_id"]

            loop.run_until_complete(engine.transition(pid, "triage"))
            r = loop.run_until_complete(
                engine.transition(
                    pid, "dispatch_work",
                    metadata={"task_name": "overridden"},
                )
            )

            child_id = r.subprocess_started
            child = engine._store.load(child_id)
            assert child.parameters["task_name"] == "overridden"
        finally:
            loop.close()

    def test_missing_param_without_metadata_raises(self, dynamic_engine):
        """Unresolved ${var} in parameter_map must raise, not silently pass
        the literal template string as the child parameter value."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = dynamic_engine.start(
                "dynamic-coordinator", {"project_name": "test-project"}
            )
            pid = result["instance_id"]

            loop.run_until_complete(dynamic_engine.transition(pid, "triage"))
            with pytest.raises(SubprocessError) as exc_info:
                loop.run_until_complete(
                    dynamic_engine.transition(pid, "dispatch_work")
                )
            msg = str(exc_info.value)
            assert "child-task" in msg
            assert "task_name" in msg
            assert "metadata" in msg
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Security: a parent cannot inject an environment-significant name into a
# dispatched child's gate shell via parameter_map.
# ---------------------------------------------------------------------------

# The child's gate reads $PATH. It declares only task_name, so its gate
# environment is restricted to task_name (see Engine._gate_params). The parent
# below maps an env-significant key (PATH) to a caller-controlled value; that
# key must be dropped from the child's gate environment because the child never
# declared it. This pins the child-side protection, which otherwise holds only
# incidentally because child gates happen to reuse _gate_params.
ENV_PROBE_CHILD = {
    "name": "env-probe-child",
    "description": "Child whose gate reads PATH",
    "version": "1.0.0",
    "parameters": [
        {"name": "task_name", "description": "What to do", "required": True},
    ],
    "states": [
        {"id": "start", "type": "initial", "transitions": ["probe"]},
        {
            "id": "probe",
            "description": "Gate reads the ambient PATH",
            "transitions": ["done"],
            "on_enter": {
                "validate": [
                    {"command": 'echo "$PATH"', "expect": "not_empty"},
                ]
            },
        },
        {"id": "done", "type": "terminal"},
    ],
}

INJECTING_PARENT = {
    "name": "injecting-coordinator",
    "description": "Coordinator that tries to set the child's PATH",
    "version": "1.0.0",
    "parameters": [
        {"name": "evil", "description": "Attacker-controlled value"},
    ],
    "states": [
        {"id": "start", "type": "initial", "transitions": ["triage"]},
        {"id": "triage", "transitions": ["dispatch_work"]},
        {
            "id": "dispatch_work",
            "type": "dispatch",
            "process": "env-probe-child",
            # Map the child's declared param, and also try to smuggle an
            # env-significant PATH key carrying the attacker value.
            "parameter_map": {
                "task_name": "do work",
                "PATH": "${evil}",
            },
            "immediate": "ready",
        },
        {"id": "ready", "transitions": ["shutdown"]},
        {"id": "shutdown", "type": "terminal"},
    ],
}


@pytest.fixture
def injecting_engine(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "injecting-coordinator.yaml").write_text(yaml.dump(INJECTING_PARENT))
    (proc_dir / "env-probe-child.yaml").write_text(yaml.dump(ENV_PROBE_CHILD))
    return Engine(tmp_path)


class TestDispatchEnvInjection:
    def test_parent_cannot_set_child_gate_path(self, injecting_engine):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            engine = injecting_engine
            result = engine.start(
                "injecting-coordinator", {"evil": "/attacker/injected"}
            )
            pid = result["instance_id"]

            loop.run_until_complete(engine.transition(pid, "triage"))
            r = loop.run_until_complete(engine.transition(pid, "dispatch_work"))
            child_id = r.subprocess_started

            # The parent DID manage to set a child parameter named PATH...
            child = engine._store.load(child_id)
            assert child.parameters.get("PATH") == "/attacker/injected"

            # ...but it must NOT reach the child's gate environment, because
            # the child does not declare PATH.
            gate = loop.run_until_complete(engine.transition(child_id, "probe"))
            gate_output = gate.validation_results[0]["output"]
            assert "/attacker/injected" not in gate_output
        finally:
            loop.close()
