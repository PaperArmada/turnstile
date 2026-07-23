"""Tests for subprocess delegation in the engine."""

import shutil
from pathlib import Path

import pytest

from turnstile_core.engine import Engine
from turnstile_core.errors import SubprocessError, TransitionError
from turnstile_core.loader import load_definition
from turnstile_core.models import (
    ProcessState,
    SkillDirective,
    StateType,
    SubprocessRouting,
)

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


# ---------------------------------------------------------------------------
# Terminal-state-aware routing (dict on_complete)
# ---------------------------------------------------------------------------


class TestSubprocessRoutingModel:
    def test_list_on_complete_targets(self):
        routing = SubprocessRouting(on_complete=["deploy"], on_fail=["rollback"])
        assert routing.on_complete_targets() == ["deploy"]

    def test_dict_on_complete_targets(self):
        routing = SubprocessRouting(
            on_complete={"done": "deploy", "failed": "fix"},
            on_fail=["rollback"],
        )
        targets = routing.on_complete_targets()
        assert set(targets) == {"deploy", "fix"}

    def test_resolve_list_ignores_child_state(self):
        routing = SubprocessRouting(on_complete=["deploy", "review"])
        assert routing.resolve_on_complete("done") == ["deploy", "review"]
        assert routing.resolve_on_complete("failed") == ["deploy", "review"]

    def test_resolve_dict_exact_match(self):
        routing = SubprocessRouting(
            on_complete={"done": "deploy", "failed": "fix"}
        )
        assert routing.resolve_on_complete("done") == ["deploy"]
        assert routing.resolve_on_complete("failed") == ["fix"]

    def test_resolve_dict_default_fallback(self):
        routing = SubprocessRouting(
            on_complete={"done": "deploy", "_default": "review"}
        )
        assert routing.resolve_on_complete("done") == ["deploy"]
        assert routing.resolve_on_complete("unknown") == ["review"]

    def test_resolve_dict_no_match_no_default(self):
        routing = SubprocessRouting(
            on_complete={"done": "deploy", "failed": "fix"}
        )
        # Falls back to all values
        result = routing.resolve_on_complete("unknown")
        assert set(result) == {"deploy", "fix"}

    def test_dict_on_complete_targets_preserve_declaration_order(self):
        """Dict-form targets are deterministic (GH #30 follow-up): mapping
        declaration order, duplicate values collapsing to their first
        occurrence."""
        routing = SubprocessRouting(
            on_complete={
                "done": "ship",
                "partial": "review",
                "failed": "ship",
                "_default": "triage",
            }
        )
        assert routing.on_complete_targets() == ["ship", "review", "triage"]

    def test_resolve_dict_all_values_fallback_preserves_declaration_order(self):
        routing = SubprocessRouting(
            on_complete={
                "done": "ship",
                "partial": "review",
                "failed": "ship",
            }
        )
        assert routing.resolve_on_complete("unknown") == [
            "ship",
            "review",
        ]

    def test_valid_subprocess_state_with_dict_routing(self):
        state = ProcessState(
            id="test",
            type=StateType.subprocess,
            process="child",
            subprocess_routing=SubprocessRouting(
                on_complete={"done": "deploy", "failed": "fix"},
                on_fail=["rollback"],
            ),
        )
        assert state.type == StateType.subprocess

    def test_load_dict_routing_definition(self):
        defn = load_definition(
            FIXTURES / "subprocess-parent-dict-routing.yaml"
        )
        test_state = defn.get_state("test")
        assert isinstance(test_state.subprocess_routing.on_complete, dict)
        assert test_state.subprocess_routing.on_complete["done"] == "deploy"
        assert test_state.subprocess_routing.on_complete["failed"] == "fix_and_retry"


@pytest.fixture()
def dict_routing_engine(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(
        FIXTURES / "subprocess-parent-dict-routing.yaml",
        proc_dir / "deploy-pipeline-v2.yaml",
    )
    shutil.copy(
        FIXTURES / "subprocess-child.yaml",
        proc_dir / "integration-tests.yaml",
    )
    return Engine(tmp_path)


class TestDictRouting:
    @pytest.mark.asyncio
    async def test_child_done_routes_to_deploy(self, dict_routing_engine):
        """Child reaching 'done' routes parent to 'deploy'."""
        engine = dict_routing_engine
        parent = engine.start("deploy-pipeline-v2", {"deploy_target": "prod"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub = await engine.transition(pid, "test")
        child_id = sub.subprocess_started

        await engine.transition(child_id, "run")
        result = await engine.transition(child_id, "done")

        assert result.parent_resumed is True
        assert result.parent_available_transitions == ["deploy"]

        # Parent can transition to deploy
        deploy = await engine.transition(pid, "deploy")
        assert deploy.success

    @pytest.mark.asyncio
    async def test_child_failed_routes_to_fix(self, dict_routing_engine):
        """Child reaching 'failed' routes parent to 'fix_and_retry'."""
        engine = dict_routing_engine
        parent = engine.start("deploy-pipeline-v2", {"deploy_target": "prod"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub = await engine.transition(pid, "test")
        child_id = sub.subprocess_started

        await engine.transition(child_id, "run")
        result = await engine.transition(child_id, "failed")

        assert result.parent_resumed is True
        assert result.parent_available_transitions == ["fix_and_retry"]

        # Parent can transition to fix_and_retry
        fix = await engine.transition(pid, "fix_and_retry")
        assert fix.success

    @pytest.mark.asyncio
    async def test_abandonment_still_uses_on_fail(self, dict_routing_engine):
        """Abandoning a child still uses on_fail routing, not on_complete dict."""
        engine = dict_routing_engine
        parent = engine.start("deploy-pipeline-v2", {"deploy_target": "prod"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub = await engine.transition(pid, "test")
        child_id = sub.subprocess_started

        result = engine.abandon(child_id, "broken")
        assert result["parent_resumed"] is True
        assert "rollback" in result["parent_available_transitions"]

    @pytest.mark.asyncio
    async def test_full_dict_routing_lifecycle(self, dict_routing_engine):
        """Full lifecycle with dict routing: fail -> fix -> rebuild -> succeed."""
        engine = dict_routing_engine
        parent = engine.start("deploy-pipeline-v2", {"deploy_target": "prod"})
        pid = parent["instance_id"]

        # First attempt: child fails
        await engine.transition(pid, "build")
        sub = await engine.transition(pid, "test")
        child_id = sub.subprocess_started
        await engine.transition(child_id, "run")
        await engine.transition(child_id, "failed")

        # Parent goes to fix_and_retry, then back to build
        await engine.transition(pid, "fix_and_retry")
        await engine.transition(pid, "build")

        # Second attempt: child succeeds
        sub2 = await engine.transition(pid, "test")
        child_id2 = sub2.subprocess_started
        await engine.transition(child_id2, "run")
        await engine.transition(child_id2, "done")

        # Parent deploys
        await engine.transition(pid, "deploy")
        done = await engine.transition(pid, "done")
        assert done.success
        assert done.new_state == "done"


@pytest.fixture()
def default_routing_engine(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(
        FIXTURES / "subprocess-parent-default-routing.yaml",
        proc_dir / "deploy-pipeline-v3.yaml",
    )
    shutil.copy(
        FIXTURES / "subprocess-child.yaml",
        proc_dir / "integration-tests.yaml",
    )
    return Engine(tmp_path)


class TestDefaultRouting:
    @pytest.mark.asyncio
    async def test_known_state_routes_directly(self, default_routing_engine):
        """Child 'done' maps to 'deploy' (explicit match)."""
        engine = default_routing_engine
        parent = engine.start("deploy-pipeline-v3", {"deploy_target": "prod"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub = await engine.transition(pid, "test")
        child_id = sub.subprocess_started

        await engine.transition(child_id, "run")
        result = await engine.transition(child_id, "done")

        assert result.parent_available_transitions == ["deploy"]

    @pytest.mark.asyncio
    async def test_unknown_state_uses_default(self, default_routing_engine):
        """Child 'failed' (not in dict) falls back to _default -> 'review'."""
        engine = default_routing_engine
        parent = engine.start("deploy-pipeline-v3", {"deploy_target": "prod"})
        pid = parent["instance_id"]

        await engine.transition(pid, "build")
        sub = await engine.transition(pid, "test")
        child_id = sub.subprocess_started

        await engine.transition(child_id, "run")
        result = await engine.transition(child_id, "failed")

        assert result.parent_available_transitions == ["review"]


# ---------------------------------------------------------------------------
# Skill directives
# ---------------------------------------------------------------------------


class TestSkillDirectiveModel:
    def test_skill_directive_defaults(self):
        sd = SkillDirective(skill="commit")
        assert sd.skill == "commit"
        assert sd.args == ""

    def test_skill_directive_with_args(self):
        sd = SkillDirective(skill="review-pr", args="--thorough")
        assert sd.args == "--thorough"

    def test_state_with_skill_directives(self):
        state = ProcessState(
            id="commit",
            skill_directives=[
                SkillDirective(skill="commit"),
                SkillDirective(skill="review-pr", args="--thorough"),
            ],
        )
        assert len(state.skill_directives) == 2

    def test_state_default_no_skill_directives(self):
        state = ProcessState(id="test")
        assert state.skill_directives == []

    def test_load_skill_directives_definition(self):
        defn = load_definition(FIXTURES / "skill-directives.yaml")
        commit_state = defn.get_state("commit")
        assert len(commit_state.skill_directives) == 1
        assert commit_state.skill_directives[0].skill == "commit"

        review_state = defn.get_state("review")
        assert len(review_state.skill_directives) == 1
        assert review_state.skill_directives[0].skill == "review-pr"
        assert review_state.skill_directives[0].args == "--thorough"


@pytest.fixture()
def skill_engine(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(
        FIXTURES / "skill-directives.yaml",
        proc_dir / "feature-with-skills.yaml",
    )
    return Engine(tmp_path)


class TestSkillDirectivesEngine:
    @pytest.mark.asyncio
    async def test_transition_returns_skill_directives(self, skill_engine):
        engine = skill_engine
        inst = engine.start("feature-with-skills", {"feature_name": "auth"})
        iid = inst["instance_id"]

        await engine.transition(iid, "implement")
        result = await engine.transition(iid, "commit")

        assert result.success
        assert len(result.skill_directives) == 1
        assert result.skill_directives[0]["skill"] == "commit"
        assert result.skill_directives[0]["args"] == ""

    @pytest.mark.asyncio
    async def test_transition_returns_skill_with_args(self, skill_engine):
        engine = skill_engine
        inst = engine.start("feature-with-skills", {"feature_name": "auth"})
        iid = inst["instance_id"]

        await engine.transition(iid, "implement")
        await engine.transition(iid, "commit")
        result = await engine.transition(iid, "review")

        assert len(result.skill_directives) == 1
        assert result.skill_directives[0]["skill"] == "review-pr"
        assert result.skill_directives[0]["args"] == "--thorough"

    @pytest.mark.asyncio
    async def test_no_skill_directives_on_normal_state(self, skill_engine):
        engine = skill_engine
        inst = engine.start("feature-with-skills", {"feature_name": "auth"})
        iid = inst["instance_id"]

        result = await engine.transition(iid, "implement")
        assert result.skill_directives == []


# ---------------------------------------------------------------------------
# Multi-level nesting (grandparent -> parent -> child)
# ---------------------------------------------------------------------------


@pytest.fixture()
def nested_engine(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(
        FIXTURES / "subprocess-grandparent.yaml",
        proc_dir / "release-pipeline.yaml",
    )
    shutil.copy(
        FIXTURES / "subprocess-parent.yaml",
        proc_dir / "deploy-pipeline.yaml",
    )
    shutil.copy(
        FIXTURES / "subprocess-child.yaml",
        proc_dir / "integration-tests.yaml",
    )
    return Engine(tmp_path)


class TestMultiLevelNesting:
    @pytest.mark.asyncio
    async def test_three_level_full_success(self, nested_engine):
        """grandparent -> parent -> child, all complete successfully."""
        engine = nested_engine

        # Start grandparent
        gp = engine.start("release-pipeline", {"release_version": "1.0"})
        gp_id = gp["instance_id"]

        # Grandparent: start -> prepare -> deploy_stage (subprocess)
        await engine.transition(gp_id, "prepare")
        gp_sub = await engine.transition(gp_id, "deploy_stage")
        assert gp_sub.subprocess_started is not None
        parent_id = gp_sub.subprocess_started

        # Parent (deploy-pipeline): start -> build -> test (subprocess)
        await engine.transition(parent_id, "build")
        p_sub = await engine.transition(parent_id, "test")
        assert p_sub.subprocess_started is not None
        child_id = p_sub.subprocess_started

        # Verify grandparent is suspended
        gp_status = engine.status(gp_id)
        assert gp_status["suspended"] is True

        # Verify parent is also suspended
        p_status = engine.status(parent_id)
        assert p_status["suspended"] is True

        # Child (integration-tests): start -> run -> done
        await engine.transition(child_id, "run")
        child_done = await engine.transition(child_id, "done")

        # Child completing resumes parent (not grandparent yet)
        assert child_done.parent_resumed is True
        assert child_done.parent_instance_id == parent_id

        # Grandparent still suspended
        gp_status = engine.status(gp_id)
        assert gp_status["suspended"] is True

        # Parent: deploy -> done
        await engine.transition(parent_id, "deploy")
        parent_done = await engine.transition(parent_id, "done")

        # Parent completing resumes grandparent
        assert parent_done.parent_resumed is True
        assert parent_done.parent_instance_id == gp_id
        assert "verify" in parent_done.parent_available_transitions

        # Grandparent: verify -> done
        await engine.transition(gp_id, "verify")
        gp_done = await engine.transition(gp_id, "done")
        assert gp_done.success
        assert gp_done.new_state == "done"

    @pytest.mark.asyncio
    async def test_three_level_child_abandonment(self, nested_engine):
        """Child abandoned: parent resumes with on_fail, grandparent still suspended."""
        engine = nested_engine

        gp = engine.start("release-pipeline", {"release_version": "2.0"})
        gp_id = gp["instance_id"]

        await engine.transition(gp_id, "prepare")
        gp_sub = await engine.transition(gp_id, "deploy_stage")
        parent_id = gp_sub.subprocess_started

        await engine.transition(parent_id, "build")
        p_sub = await engine.transition(parent_id, "test")
        child_id = p_sub.subprocess_started

        # Abandon child
        abandon_result = engine.abandon(child_id, "tests broken")
        assert abandon_result["parent_resumed"] is True
        assert abandon_result["parent_instance_id"] == parent_id

        # Grandparent still suspended (parent hasn't completed)
        gp_status = engine.status(gp_id)
        assert gp_status["suspended"] is True

        # Parent can rollback
        await engine.transition(parent_id, "rollback")
        parent_done = await engine.transition(parent_id, "done")

        # Now grandparent resumes
        assert parent_done.parent_resumed is True
        assert parent_done.parent_instance_id == gp_id

    @pytest.mark.asyncio
    async def test_three_level_parent_abandonment(self, nested_engine):
        """Parent abandoned: grandparent resumes with on_fail."""
        engine = nested_engine

        gp = engine.start("release-pipeline", {"release_version": "3.0"})
        gp_id = gp["instance_id"]

        await engine.transition(gp_id, "prepare")
        gp_sub = await engine.transition(gp_id, "deploy_stage")
        parent_id = gp_sub.subprocess_started

        # Abandon parent directly
        result = engine.abandon(parent_id, "deploy broken")
        assert result["parent_resumed"] is True
        assert result["parent_instance_id"] == gp_id
        assert "abort" in result["parent_available_transitions"]

        # Grandparent goes to abort -> aborted
        await engine.transition(gp_id, "abort")
        gp_aborted = await engine.transition(gp_id, "aborted")
        assert gp_aborted.success
        assert gp_aborted.new_state == "aborted"

    @pytest.mark.asyncio
    async def test_three_level_parameter_propagation(self, nested_engine):
        """Parameters flow through all three levels."""
        engine = nested_engine

        gp = engine.start("release-pipeline", {"release_version": "4.0"})
        gp_id = gp["instance_id"]

        await engine.transition(gp_id, "prepare")
        gp_sub = await engine.transition(gp_id, "deploy_stage")
        parent_id = gp_sub.subprocess_started

        # Parent should have deploy_target from grandparent's parameter_map
        p_status = engine.status(parent_id)
        assert p_status["parameters"]["deploy_target"] == "staging-4.0"

        await engine.transition(parent_id, "build")
        p_sub = await engine.transition(parent_id, "test")
        child_id = p_sub.subprocess_started

        # Child should have target_env from parent's parameter_map
        c_status = engine.status(child_id)
        assert c_status["parameters"]["target_env"] == "staging-4.0"

    @pytest.mark.asyncio
    async def test_three_level_status_shows_all(self, nested_engine):
        """All three active instances appear in status listing."""
        engine = nested_engine

        gp = engine.start("release-pipeline", {"release_version": "5.0"})
        gp_id = gp["instance_id"]

        await engine.transition(gp_id, "prepare")
        gp_sub = await engine.transition(gp_id, "deploy_stage")
        parent_id = gp_sub.subprocess_started

        await engine.transition(parent_id, "build")
        p_sub = await engine.transition(parent_id, "test")
        child_id = p_sub.subprocess_started

        all_status = engine.status()
        ids = {s["instance_id"] for s in all_status}
        assert {gp_id, parent_id, child_id} == ids
