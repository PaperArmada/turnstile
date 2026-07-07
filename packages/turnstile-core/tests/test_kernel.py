"""Tests for turnstile_core.kernel.plan — pure transition semantics.

Everything here runs without a filesystem, a store, or an engine:
the kernel is a function of (definition, instance, request).
"""

import pytest

from turnstile_core.definition.model import ProcessDefinition
from turnstile_core.errors import SubprocessError, TransitionError
from turnstile_core.instance.model import ProcessInstance
from turnstile_core.kernel.plan import (
    check_required_metadata,
    coerce_extras,
    gate_parameters,
    legal_targets,
    plan_signal,
    plan_transition,
    resolve_child_parameters,
    resolve_parent_transitions,
    summarize,
)


def make_definition(**overrides) -> ProcessDefinition:
    data = {
        "name": "kernel-test",
        "version": "1.0.0",
        "states": [
            {"id": "start", "type": "initial", "transitions": ["work"]},
            {
                "id": "work",
                "transitions": ["review", "done"],
                "required_metadata": [
                    {"key": "commit", "description": "commit sha"},
                ],
            },
            {"id": "review", "transitions": ["done", "work"]},
            {"id": "done", "type": "terminal"},
        ],
    }
    data.update(overrides)
    return ProcessDefinition(**data)


def make_instance(state: str = "start", **overrides) -> ProcessInstance:
    data = {
        "instance_id": "abc123",
        "process_name": "kernel-test",
        "current_state": state,
        "started_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:30:00+00:00",
        "parameters": {"feature": "x"},
    }
    data.update(overrides)
    return ProcessInstance(**data)


class TestPlanTransition:
    def test_legal_transition(self):
        plan = plan_transition(make_definition(), make_instance(), "work",
                               metadata={"commit": "deadbeef"})
        assert plan.current.id == "start"
        assert plan.target.id == "work"
        assert plan.gate_params == {"feature": "x", "instance_id": "abc123"}

    def test_illegal_transition(self):
        with pytest.raises(TransitionError, match="not allowed"):
            plan_transition(make_definition(), make_instance(), "done")

    def test_unknown_target(self):
        with pytest.raises(TransitionError, match="not allowed"):
            plan_transition(make_definition(), make_instance(), "nowhere")

    def test_suspended_instance_blocked(self):
        inst = make_instance(suspended=True, child_instance_id="kid001")
        with pytest.raises(TransitionError, match="suspended"):
            plan_transition(make_definition(), inst, "work")

    def test_waiting_instance_blocked(self):
        inst = make_instance(waiting=True)
        with pytest.raises(TransitionError, match="waiting for a signal"):
            plan_transition(make_definition(), inst, "work")

    def test_required_metadata_enforced(self):
        inst = make_instance(state="work")
        with pytest.raises(TransitionError, match="requires metadata"):
            plan_transition(make_definition(), inst, "review", metadata={})

    def test_required_metadata_satisfied(self):
        inst = make_instance(state="work")
        plan = plan_transition(
            make_definition(), inst, "review", metadata={"commit": "abc"}
        )
        assert plan.target.id == "review"


class TestRequiredMetadata:
    def test_missing_keys_listed_with_descriptions(self):
        defn = make_definition()
        work = defn.get_state("work")
        with pytest.raises(TransitionError, match="'commit' \\(commit sha\\)"):
            check_required_metadata(work, None)

    def test_no_requirements_passes(self):
        defn = make_definition()
        check_required_metadata(defn.get_state("start"), None)


class TestLegalTargets:
    def test_normal_state_uses_transitions(self):
        defn = make_definition()
        assert legal_targets(defn.get_state("work")) == ["review", "done"]

    def test_subprocess_state_uses_routing(self):
        defn = ProcessDefinition(**{
            "name": "sub-test",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["delegate"]},
                {
                    "id": "delegate",
                    "type": "subprocess",
                    "process": "child",
                    "subprocess_routing": {
                        "on_complete": ["done"],
                        "on_fail": ["start"],
                    },
                },
                {"id": "done", "type": "terminal"},
            ],
        })
        assert set(legal_targets(defn.get_state("delegate"))) == {"done", "start"}


class TestPlanSignal:
    def wait_definition(self) -> ProcessDefinition:
        return ProcessDefinition(**{
            "name": "wait-test",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["hold"]},
                {
                    "id": "hold",
                    "type": "wait",
                    "signal": {
                        "name": "approval",
                        "required_fields": [{"key": "approved"}],
                    },
                    "transitions": ["done"],
                },
                {"id": "done", "type": "terminal"},
            ],
        })

    def test_valid_signal_with_target(self):
        inst = make_instance(state="hold", waiting=True)
        plan = plan_signal(
            self.wait_definition(), inst, "approval",
            {"approved": True}, "done",
        )
        assert plan.current.id == "hold"
        assert plan.target.id == "done"

    def test_valid_signal_without_target(self):
        inst = make_instance(state="hold", waiting=True)
        plan = plan_signal(
            self.wait_definition(), inst, "approval", {"approved": True}
        )
        assert plan.target is None

    def test_not_waiting_rejected(self):
        inst = make_instance(state="hold", waiting=False)
        with pytest.raises(TransitionError, match="not waiting"):
            plan_signal(self.wait_definition(), inst, "approval", {})

    def test_wrong_signal_name(self):
        inst = make_instance(state="hold", waiting=True)
        with pytest.raises(TransitionError, match="Expected signal 'approval'"):
            plan_signal(self.wait_definition(), inst, "nope", {})

    def test_missing_required_fields(self):
        inst = make_instance(state="hold", waiting=True)
        with pytest.raises(TransitionError, match="missing required fields"):
            plan_signal(self.wait_definition(), inst, "approval", {})

    def test_invalid_target(self):
        inst = make_instance(state="hold", waiting=True)
        with pytest.raises(TransitionError, match="not a valid transition"):
            plan_signal(
                self.wait_definition(), inst, "approval",
                {"approved": True}, "start",
            )


class TestResolveChildParameters:
    def child_definition(self) -> ProcessDefinition:
        return ProcessDefinition(**{
            "name": "child",
            "parameters": [
                {"name": "task", "required": True},
                {"name": "priority", "required": False, "default": "low"},
            ],
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        })

    def dispatch_state(self, parameter_map):
        defn = ProcessDefinition(**{
            "name": "parent",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["send"]},
                {
                    "id": "send",
                    "type": "dispatch",
                    "process": "child",
                    "immediate": "done",
                    "parameter_map": parameter_map,
                },
                {"id": "done", "type": "terminal"},
            ],
        })
        return defn.get_state("send")

    def test_substitutes_and_defaults(self):
        state = self.dispatch_state({"task": "review ${feature}"})
        params = resolve_child_parameters(
            state, {"feature": "auth"}, self.child_definition()
        )
        assert params == {"task": "review auth", "priority": "low"}

    def test_unresolved_placeholder_fails_fast(self):
        state = self.dispatch_state({"task": "review ${missing}"})
        with pytest.raises(SubprocessError, match="unresolved parameter"):
            resolve_child_parameters(state, {"feature": "auth"},
                                     self.child_definition())

    def test_missing_required_parameter(self):
        state = self.dispatch_state({})
        with pytest.raises(SubprocessError, match="requires parameter 'task'"):
            resolve_child_parameters(
                state, {}, self.child_definition(), kind="Dispatch process"
            )


class TestResolveParentTransitions:
    def parent_definition(self, on_complete) -> ProcessDefinition:
        return ProcessDefinition(**{
            "name": "parent",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["delegate"]},
                {
                    "id": "delegate",
                    "type": "subprocess",
                    "process": "child",
                    "subprocess_routing": {
                        "on_complete": on_complete,
                        "on_fail": ["start"],
                    },
                },
                {"id": "merge", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        })

    def test_list_routing(self):
        defn = self.parent_definition(["merge"])
        parent = make_instance(state="delegate", suspended=True)
        assert resolve_parent_transitions(defn, parent, "completed", "done") == ["merge"]

    def test_dict_routing_by_child_terminal(self):
        defn = self.parent_definition({"done": "merge", "_default": "done"})
        parent = make_instance(state="delegate", suspended=True)
        assert resolve_parent_transitions(defn, parent, "completed", "done") == ["merge"]
        assert resolve_parent_transitions(defn, parent, "completed", "other") == ["done"]

    def test_abandoned_routes_to_on_fail(self):
        defn = self.parent_definition(["merge"])
        parent = make_instance(state="delegate", suspended=True)
        assert resolve_parent_transitions(defn, parent, "abandoned") == ["start"]

    def test_no_routing_returns_none(self):
        defn = make_definition()
        parent = make_instance(state="work")
        assert resolve_parent_transitions(defn, parent, "completed", "done") is None


class TestHelpers:
    def test_gate_parameters_include_instance_id(self):
        params = gate_parameters(make_instance())
        assert params["instance_id"] == "abc123"
        assert params["feature"] == "x"

    def test_coerce_extras_scalars_only(self):
        extras = coerce_extras({
            "s": "text", "i": 3, "f": 1.5, "b": True,
            "skip_list": [1], "skip_dict": {"a": 1}, "skip_none": None,
        })
        assert extras == {"s": "text", "i": "3", "f": "1.5", "b": "True"}

    def test_coerce_extras_none(self):
        assert coerce_extras(None) == {}

    def test_summarize_counts(self):
        inst = make_instance(state="done", history=[
            {"from": "start", "to": "work", "at": "2026-01-01T00:05:00+00:00",
             "validations": [{"passed": True}, {"passed": False}]},
            {"from": "work", "to": "done", "at": "2026-01-01T00:20:00+00:00"},
        ])
        summary = summarize(inst)
        assert summary["states_visited"] == ["start", "work", "done"]
        assert summary["transition_count"] == 2
        assert summary["validations_run"] == 2
        assert summary["validations_passed"] == 1
        assert summary["validations_failed"] == 1
        assert summary["elapsed"] == "0:30:00"
