"""Tests for subprocess delegation in the engine."""

import shutil
from pathlib import Path

import pytest

from turnstile_core.engine import Engine
from turnstile_core.errors import SubprocessError, TransitionError
from turnstile_core.loader import load_definition
from turnstile_core.models import ProcessState, StateType, SubprocessRouting

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestSubprocessModels:
    def test_subprocess_state_requires_process(self):
        with pytest.raises(ValueError, match="must have 'process'"):
            ProcessState(
                id="test",
                type=StateType.subprocess,
                subprocess_routing=SubprocessRouting(on_complete=["done"]),
            )

    def test_subprocess_state_requires_routing(self):
        with pytest.raises(ValueError, match="must have 'subprocess_routing'"):
            ProcessState(
                id="test",
                type=StateType.subprocess,
                process="child",
            )

    def test_subprocess_state_no_transitions(self):
        with pytest.raises(ValueError, match="must not have 'transitions'"):
            ProcessState(
                id="test",
                type=StateType.subprocess,
                process="child",
                transitions=["next"],
                subprocess_routing=SubprocessRouting(on_complete=["next"]),
            )

    def test_normal_state_no_subprocess_routing(self):
        with pytest.raises(ValueError, match="must not have 'subprocess_routing'"):
            ProcessState(
                id="test",
                type=StateType.normal,
                subprocess_routing=SubprocessRouting(on_complete=["done"]),
            )

    def test_valid_subprocess_state(self):
        state = ProcessState(
            id="test",
            type=StateType.subprocess,
            process="child",
            subprocess_routing=SubprocessRouting(
                on_complete=["deploy"],
                on_fail=["rollback"],
            ),
        )
        assert state.type == StateType.subprocess

    def test_load_subprocess_definition(self):
        defn = load_definition(FIXTURES / "subprocess-parent.yaml")
        test_state = defn.get_state("test")
        assert test_state.type == StateType.subprocess
        assert test_state.process == "integration-tests"
        assert test_state.subprocess_routing.on_complete == ["deploy"]
        assert test_state.subprocess_routing.on_fail == ["rollback"]


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(
        FIXTURES / "subprocess-parent.yaml",
        proc_dir / "deploy-pipeline.yaml",
    )
    shutil.copy(
        FIXTURES / "subprocess-child.yaml",
        proc_dir / "integration-tests.yaml",
    )
    return Engine(tmp_path)


class TestSubprocessLifecycle:
    @pytest.mark.asyncio
    async def test_transition_to_subprocess_starts_child(self, engine):
        parent = engine.start("deploy-pipeline", {"deploy_target": "staging"})
        pid = parent["instance_id"]

        # Move to build
        await engine.transition(pid, "build")

        # Move to test (subprocess state)
        result = await engine.transition(pid, "test")
        assert result.success
        assert result.subprocess_started is not None
        assert result.message.startswith("Subprocess")

    @pytest.mark.asyncio
    async def test_parent_suspended_after_subprocess_start(self, engine):
        parent = engine.start("deploy-pipeline", {"deploy_target": "staging"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        result = await engine.transition(pid, "test")

        # Parent should be suspended
        status = engine.status(pid)
        assert status["suspended"] is True
        assert status["child_instance_id"] == result.subprocess_started

    @pytest.mark.asyncio
    async def test_suspended_parent_blocks_transitions(self, engine):
        parent = engine.start("deploy-pipeline", {"deploy_target": "staging"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        await engine.transition(pid, "test")

        with pytest.raises(TransitionError, match="suspended"):
            await engine.transition(pid, "deploy")

    @pytest.mark.asyncio
    async def test_child_has_parent_link(self, engine):
        parent = engine.start("deploy-pipeline", {"deploy_target": "staging"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        result = await engine.transition(pid, "test")

        child_status = engine.status(result.subprocess_started)
        assert child_status["parent_instance_id"] == pid

    @pytest.mark.asyncio
    async def test_child_parameter_mapping(self, engine):
        parent = engine.start("deploy-pipeline", {"deploy_target": "staging"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        result = await engine.transition(pid, "test")

        child_status = engine.status(result.subprocess_started)
        assert child_status["parameters"]["target_env"] == "staging"

    @pytest.mark.asyncio
    async def test_child_completion_resumes_parent(self, engine):
        parent = engine.start("deploy-pipeline", {"deploy_target": "staging"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub_result = await engine.transition(pid, "test")
        child_id = sub_result.subprocess_started

        # Advance child to terminal
        await engine.transition(child_id, "run")
        result = await engine.transition(child_id, "done")

        assert result.parent_resumed is True
        assert result.parent_instance_id == pid
        assert "deploy" in result.parent_available_transitions

        # Parent should no longer be suspended
        parent_status = engine.status(pid)
        assert parent_status["suspended"] is False

    @pytest.mark.asyncio
    async def test_parent_can_advance_after_child_completes(self, engine):
        parent = engine.start("deploy-pipeline", {"deploy_target": "staging"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub_result = await engine.transition(pid, "test")
        child_id = sub_result.subprocess_started

        # Complete child
        await engine.transition(child_id, "run")
        await engine.transition(child_id, "done")

        # Now parent can advance to deploy (on_complete target)
        deploy_result = await engine.transition(pid, "deploy")
        assert deploy_result.success
        assert deploy_result.new_state == "deploy"

    @pytest.mark.asyncio
    async def test_child_terminal_always_uses_on_complete(self, engine):
        """Any terminal state in child uses on_complete routing.
        on_fail is only for abandonment."""
        parent = engine.start("deploy-pipeline", {"deploy_target": "staging"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub_result = await engine.transition(pid, "test")
        child_id = sub_result.subprocess_started

        # Child reaches "failed" terminal (still a completion)
        await engine.transition(child_id, "run")
        result = await engine.transition(child_id, "failed")

        assert result.parent_resumed is True
        # Any terminal = on_complete; only abandon = on_fail
        assert "deploy" in result.parent_available_transitions

    @pytest.mark.asyncio
    async def test_parent_can_rollback_after_child_abandonment(self, engine):
        """Abandoning a child uses on_fail routing, enabling rollback."""
        parent = engine.start("deploy-pipeline", {"deploy_target": "staging"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub_result = await engine.transition(pid, "test")
        child_id = sub_result.subprocess_started

        engine.abandon(child_id, "tests broken")

        # Parent should be able to go to rollback (on_fail routing)
        rollback = await engine.transition(pid, "rollback")
        assert rollback.success
        assert rollback.new_state == "rollback"

    @pytest.mark.asyncio
    async def test_child_abandonment_resumes_parent(self, engine):
        parent = engine.start("deploy-pipeline", {"deploy_target": "staging"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub_result = await engine.transition(pid, "test")
        child_id = sub_result.subprocess_started

        # Abandon the child
        result = engine.abandon(child_id, "tests broken")
        assert result["parent_resumed"] is True
        assert result["parent_instance_id"] == pid
        assert "rollback" in result["parent_available_transitions"]

    @pytest.mark.asyncio
    async def test_full_success_lifecycle(self, engine):
        """Full lifecycle: start -> build -> test(subprocess) -> deploy -> done."""
        parent = engine.start("deploy-pipeline", {"deploy_target": "prod"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub_result = await engine.transition(pid, "test")
        child_id = sub_result.subprocess_started

        await engine.transition(child_id, "run")
        await engine.transition(child_id, "done")

        await engine.transition(pid, "deploy")
        done_result = await engine.transition(pid, "done")
        assert done_result.success
        assert done_result.new_state == "done"

    @pytest.mark.asyncio
    async def test_full_failure_lifecycle(self, engine):
        """Full lifecycle with abandon: start -> build -> test -> rollback -> done."""
        parent = engine.start("deploy-pipeline", {"deploy_target": "prod"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub_result = await engine.transition(pid, "test")
        child_id = sub_result.subprocess_started

        engine.abandon(child_id, "tests broken")

        await engine.transition(pid, "rollback")
        done_result = await engine.transition(pid, "done")
        assert done_result.success

    @pytest.mark.asyncio
    async def test_status_lists_both_parent_and_child(self, engine):
        parent = engine.start("deploy-pipeline", {"deploy_target": "staging"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub_result = await engine.transition(pid, "test")

        all_status = engine.status()
        instance_ids = [s["instance_id"] for s in all_status]
        assert pid in instance_ids
        assert sub_result.subprocess_started in instance_ids
