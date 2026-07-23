"""Engine.trajectory() and the enriched Engine.history().

trajectory() is the full reviewable record of an instance: the envelope
(status, current state, parameters, timestamps) plus every transition and
every override, searched across active and archived instances. history()
stays the lean transition list; role and session_id are additive keys that
appear only when non-empty, matching the metadata pattern, so existing
consumers see unchanged entry shapes.
"""

import shutil
from pathlib import Path

import pytest

from turnstile_core.engine import Engine
from turnstile_core.errors import InstanceNotFoundError

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def project(tmp_path) -> Path:
    """Temporary project with the simple and roled process definitions."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
    shutil.copy(FIXTURES / "roled.yaml", proc_dir / "roled.yaml")
    return tmp_path


@pytest.fixture()
def engine(project: Path) -> Engine:
    return Engine(project)


class TestTrajectoryEnvelope:
    @pytest.mark.asyncio
    async def test_envelope_describes_the_instance(self, engine: Engine):
        started = engine.start("simple", {"task_name": "audit me"})
        iid = started["instance_id"]
        await engine.transition(iid, "working")

        record = engine.trajectory(iid)
        assert record["instance_id"] == iid
        assert record["process_name"] == "simple"
        assert record["status"] == "active"
        assert record["current_state"] == "working"
        assert record["parameters"] == {"task_name": "audit me"}
        assert record["waiting"] is False
        assert record["started_at"]
        assert record["updated_at"]

    def test_fresh_instance_has_empty_transitions_and_overrides(
        self, engine: Engine
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]

        record = engine.trajectory(iid)
        assert record["transitions"] == []
        assert record["overrides"] == []

    @pytest.mark.asyncio
    async def test_completed_instance_remains_reviewable(self, engine: Engine):
        """After the terminal transition archives the instance, trajectory
        still finds it and reports the completed status."""
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "working")
        await engine.transition(iid, "review")
        await engine.transition(iid, "done")

        record = engine.trajectory(iid)
        assert record["status"] == "completed"
        assert record["current_state"] == "done"
        assert len(record["transitions"]) == 3

    @pytest.mark.asyncio
    async def test_abandoned_instance_remains_reviewable(self, engine: Engine):
        """trajectory searches all three directories; an abandoned
        instance keeps its record and reports the abandoned status."""
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "working")
        engine.abandon(iid, "requirements changed")

        record = engine.trajectory(iid)
        assert record["status"] == "abandoned"
        assert record["current_state"] == "working"
        assert len(record["transitions"]) == 1

    def test_subprocess_linkage_keys_present_on_plain_instance(
        self, engine: Engine
    ):
        """The envelope carries the subprocess-linkage and signal fields
        even for an instance with no parent, child, or signal: consumers
        can rely on the keys existing."""
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]

        record = engine.trajectory(iid)
        assert record["parent_instance_id"] is None
        assert record["parent_state_id"] is None
        assert record["child_instance_id"] is None
        assert record["suspended"] is False
        assert record["signal_data"] is None

    def test_unknown_instance_raises(self, engine: Engine):
        with pytest.raises(InstanceNotFoundError):
            engine.trajectory("nonexistent")


class TestTrajectoryTransitions:
    @pytest.mark.asyncio
    async def test_transition_record_is_complete(self, engine: Engine):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.transition(
            iid, "working", metadata={"pr": "42"}, session_id="sess-1"
        )

        [t] = engine.trajectory(iid)["transitions"]
        assert t["from_state"] == "start"
        assert t["to_state"] == "working"
        assert t["timestamp"]
        assert t["session_id"] == "sess-1"
        assert t["metadata"] == {"pr": "42"}
        assert t["validations"] == []

    @pytest.mark.asyncio
    async def test_empty_attribution_keys_are_still_present(
        self, engine: Engine
    ):
        """Unlike history(), trajectory transitions always carry
        triggered_by/role/session_id/metadata, even when empty. The CLI
        indexes these keys unconditionally, so their presence is part of
        the contract."""
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "working")

        [t] = engine.trajectory(iid)["transitions"]
        assert t["triggered_by"] == ""
        assert t["role"] == ""
        assert t["session_id"] == ""
        assert t["metadata"] == {}

    @pytest.mark.asyncio
    async def test_gate_results_recorded_on_transitions(self, engine: Engine):
        """working -> review runs working's on_exit gate and review's
        on_enter gate; both results land on that transition."""
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "working")
        await engine.transition(iid, "review")

        record = engine.trajectory(iid)
        validations = record["transitions"][1]["validations"]
        assert len(validations) == 2
        assert all(v["passed"] for v in validations)
        assert {v["message"] for v in validations} == {
            "Must have output",
            "Must confirm",
        }


class TestTrajectoryOverrides:
    @pytest.mark.asyncio
    async def test_skip_recorded_as_override_and_transition(
        self, engine: Engine
    ):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.skip(
            iid, "review", "gates run by hand", session_id="sess-123"
        )

        record = engine.trajectory(iid)
        [o] = record["overrides"]
        assert o["from_state"] == "start"
        assert o["to_state"] == "review"
        assert o["reason"] == "gates run by hand"
        assert o["at"]
        # The session id is the correlation identity for the override
        assert o["triggered_by"] == "sess-123"

        [t] = record["transitions"]
        assert t["triggered_by"] == "skip: gates run by hand"

    @pytest.mark.asyncio
    async def test_skip_without_session_leaves_attribution_empty(
        self, engine: Engine
    ):
        """A skip from a context with no session id records an empty
        triggered_by (rendered as 'unknown' downstream), not a fabricated
        actor."""
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.skip(iid, "review", "no session context")

        [o] = engine.trajectory(iid)["overrides"]
        assert o["triggered_by"] == ""


class TestHistoryAttribution:
    """history() adds role/session_id additively: keys appear only when
    the underlying entry recorded a non-empty value."""

    @pytest.mark.asyncio
    async def test_session_id_appears_only_when_recorded(self, engine: Engine):
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]
        await engine.transition(iid, "working", session_id="sess-9")
        await engine.transition(iid, "review")

        hist = engine.history(iid)
        assert hist[0]["session_id"] == "sess-9"
        assert "session_id" not in hist[1]
        # simple has no roles anywhere: the key never appears
        assert "role" not in hist[0]
        assert "role" not in hist[1]

    @pytest.mark.asyncio
    async def test_role_appears_only_for_role_bearing_states(
        self, engine: Engine
    ):
        iid = engine.start("roled")["instance_id"]
        await engine.transition(iid, "implement")
        await engine.transition(iid, "done")

        hist = engine.history(iid)
        assert hist[0]["role"] == "implementer"
        assert "role" not in hist[1]
