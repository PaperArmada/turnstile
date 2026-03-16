"""Tests for wait states and signal delivery."""

import shutil
from pathlib import Path

import pytest
import yaml

from turnstile_core.engine import Engine
from turnstile_core.errors import TransitionError
from turnstile_core.models import (
    ProcessState,
    SignalField,
    SignalSpec,
    StateType,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestWaitModels:
    def test_wait_state_requires_signal(self):
        with pytest.raises(ValueError, match="must have 'signal'"):
            ProcessState(
                id="test",
                type=StateType.wait,
                transitions=["next"],
            )

    def test_wait_state_requires_transitions(self):
        with pytest.raises(ValueError, match="must have transitions"):
            ProcessState(
                id="test",
                type=StateType.wait,
                signal=SignalSpec(name="approval"),
            )

    def test_normal_state_no_signal(self):
        with pytest.raises(ValueError, match="must not have 'signal'"):
            ProcessState(
                id="test",
                type=StateType.normal,
                signal=SignalSpec(name="approval"),
            )

    def test_valid_wait_state(self):
        state = ProcessState(
            id="review",
            type=StateType.wait,
            signal=SignalSpec(
                name="review_complete",
                required_fields=[
                    SignalField(key="approved"),
                    SignalField(key="comments", description="Reviewer feedback"),
                ],
            ),
            transitions=["approved", "rejected"],
        )
        assert state.signal.name == "review_complete"
        assert len(state.signal.required_fields) == 2


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


WAIT_PROCESS = {
    "name": "wait-test",
    "description": "Test process with a wait state",
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
                "required_fields": [
                    {"key": "approved"},
                    {"key": "comments"},
                ],
            },
            "transitions": ["approved", "rejected"],
        },
        {"id": "approved", "type": "terminal"},
        {"id": "rejected", "transitions": ["work"]},
    ],
}


@pytest.fixture
def engine(tmp_path):
    """Create an engine with the wait-test process."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "wait-test.yaml").write_text(yaml.dump(WAIT_PROCESS))
    return Engine(tmp_path)


@pytest.fixture
def waiting_instance(engine):
    """Create an instance already in the wait state."""
    result = engine.start("wait-test")
    iid = result["instance_id"]

    import asyncio
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine.transition(iid, "work"))
        r = loop.run_until_complete(engine.transition(iid, "review"))
    finally:
        loop.close()

    assert r.success
    assert r.new_state == "review"
    assert r.available_transitions == []
    assert "Waiting for signal" in r.message
    return iid


class TestWaitTransition:
    def test_transition_to_wait_sets_waiting(self, engine):
        result = engine.start("wait-test")
        iid = result["instance_id"]

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(engine.transition(iid, "work"))
            r = loop.run_until_complete(engine.transition(iid, "review"))
        finally:
            loop.close()

        assert r.success
        assert r.new_state == "review"
        assert r.available_transitions == []
        assert r.role == "reviewer"
        assert "Waiting for signal 'review_complete'" in r.message

    def test_transition_blocked_while_waiting(self, waiting_instance, engine):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(TransitionError, match="waiting for a signal"):
                loop.run_until_complete(
                    engine.transition(waiting_instance, "approved")
                )
        finally:
            loop.close()

    def test_status_shows_waiting(self, waiting_instance, engine):
        status = engine.status(waiting_instance)
        assert status["waiting"] is True
        assert status["waiting_for_signal"] == "review_complete"
        assert status["available_transitions"] == []


class TestSignalDelivery:
    def test_signal_with_target(self, waiting_instance, engine):
        result = engine.receive_signal(
            waiting_instance, "review_complete",
            {"approved": True, "comments": "Looks good"},
            target_state="approved",
        )
        assert result["success"]
        assert result["new_state"] == "approved"
        assert result["signal_data"]["approved"] is True

    def test_signal_without_target(self, waiting_instance, engine):
        result = engine.receive_signal(
            waiting_instance, "review_complete",
            {"approved": False, "comments": "Needs work"},
        )
        assert result["success"]
        assert result["new_state"] == "review"
        assert result["available_transitions"] == ["approved", "rejected"]

    def test_signal_wrong_name(self, waiting_instance, engine):
        with pytest.raises(TransitionError, match="Expected signal"):
            engine.receive_signal(
                waiting_instance, "wrong_signal",
                {"approved": True, "comments": ""},
            )

    def test_signal_missing_fields(self, waiting_instance, engine):
        with pytest.raises(TransitionError, match="missing required fields"):
            engine.receive_signal(
                waiting_instance, "review_complete",
                {"approved": True},  # missing 'comments'
            )

    def test_signal_invalid_target(self, waiting_instance, engine):
        with pytest.raises(TransitionError, match="not a valid transition"):
            engine.receive_signal(
                waiting_instance, "review_complete",
                {"approved": True, "comments": ""},
                target_state="nonexistent",
            )

    def test_signal_not_waiting(self, engine):
        result = engine.start("wait-test")
        iid = result["instance_id"]

        with pytest.raises(TransitionError, match="not waiting"):
            engine.receive_signal(
                iid, "review_complete",
                {"approved": True, "comments": ""},
            )

    def test_signal_records_history(self, waiting_instance, engine):
        # Signal to "rejected" (non-terminal) so we can inspect history
        engine.receive_signal(
            waiting_instance, "review_complete",
            {"approved": False, "comments": "Needs work"},
            target_state="rejected",
            session_id="test-session",
        )
        instance = engine._store.load(waiting_instance)
        last = instance.history[-1]
        assert last.from_state == "review"
        assert last.to_state == "rejected"
        assert last.triggered_by == "signal: review_complete"
        assert last.session_id == "test-session"
        assert last.metadata["signal_data"]["approved"] is False
        assert last.metadata["signal_data"]["comments"] == "Needs work"

    def test_signal_then_transition(self, waiting_instance, engine):
        """Signal without target, then normal transition."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            # Deliver signal without target
            engine.receive_signal(
                waiting_instance, "review_complete",
                {"approved": False, "comments": "Needs work"},
            )

            # Now normal transition should work (waiting cleared)
            r = loop.run_until_complete(
                engine.transition(waiting_instance, "rejected")
            )
            assert r.success
            assert r.new_state == "rejected"
        finally:
            loop.close()
