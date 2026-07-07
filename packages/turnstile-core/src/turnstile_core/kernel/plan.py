"""Pure transition semantics: decide, don't do.

The kernel answers every question about *what should happen* — is this
move legal, which gates guard it, what parameters does a child process
get, what does arriving in the target state entail — without touching
disk, spawning processes, or firing notifications. The runtime layer
executes what the kernel decides.

Because everything here is a pure function of (definition, instance,
request), the whole semantics of the engine can be tested without a
filesystem, and the transition and signal paths cannot drift apart:
they share one implementation of legality, metadata requirements, and
child-parameter resolution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from turnstile_core.definition.model import (
    ProcessDefinition,
    ProcessState,
    StateType,
)
from turnstile_core.errors import SubprocessError, TransitionError
from turnstile_core.instance.model import ProcessInstance
from turnstile_core.templating import substitute_params, unresolved_placeholders


# ---------------------------------------------------------------------------
# Legality
# ---------------------------------------------------------------------------


def legal_targets(state: ProcessState) -> list[str]:
    """All state IDs reachable from a state.

    For subprocess states the reachable set comes from the routing
    block (on_complete/on_fail); for every other type it is the
    transitions list.
    """
    if state.type == StateType.subprocess and state.subprocess_routing:
        return (
            state.subprocess_routing.on_complete_targets()
            + state.subprocess_routing.on_fail
        )
    return state.transitions


def gate_parameters(instance: ProcessInstance) -> dict[str, str]:
    """The substitution context for gate commands and action hooks:
    declared parameters plus the instance ID."""
    return {**instance.parameters, "instance_id": instance.instance_id}


# ---------------------------------------------------------------------------
# Transition planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionPlan:
    """Everything the runtime needs to execute one legal transition."""

    current: ProcessState
    target: ProcessState
    gate_params: dict[str, str]


def plan_transition(
    defn: ProcessDefinition,
    instance: ProcessInstance,
    target_id: str,
    metadata: dict[str, Any] | None = None,
) -> TransitionPlan:
    """Validate a transition request and plan its execution.

    Raises TransitionError for any illegal request. Performs no I/O.
    """
    if instance.suspended:
        raise TransitionError(
            f"Instance is suspended waiting for subprocess "
            f"'{instance.child_instance_id}'. Complete or abandon "
            f"the child process first."
        )

    if instance.waiting:
        raise TransitionError(
            "Instance is waiting for a signal. Use receive_signal() "
            "to deliver the signal and advance the state."
        )

    current = defn.get_state(instance.current_state)
    if current is None:
        raise TransitionError(
            f"Current state '{instance.current_state}' not found in definition"
        )

    allowed = legal_targets(current)
    if target_id not in allowed:
        if current.type == StateType.subprocess:
            raise TransitionError(
                f"Transition from subprocess state '{current.id}' to "
                f"'{target_id}' is not allowed. "
                f"Legal targets: {allowed}"
            )
        raise TransitionError(
            f"Transition from '{current.id}' to '{target_id}' is not allowed. "
            f"Legal transitions: {allowed}"
        )

    target = defn.get_state(target_id)
    if target is None:
        raise TransitionError(
            f"Target state '{target_id}' not found in definition"
        )

    check_required_metadata(current, metadata)

    # Fail fast on a broken dispatch wiring before any side effects.
    if target.type == StateType.dispatch:
        if defn.get_state(target.immediate) is None:
            raise TransitionError(
                f"Dispatch immediate target '{target.immediate}' "
                f"not found in definition"
            )

    return TransitionPlan(
        current=current,
        target=target,
        gate_params=gate_parameters(instance),
    )


def check_required_metadata(
    state: ProcessState, metadata: dict[str, Any] | None
) -> None:
    """Raise TransitionError if the state's required metadata is missing."""
    if not state.required_metadata:
        return
    provided = metadata or {}
    missing = [
        rm.key for rm in state.required_metadata if rm.key not in provided
    ]
    if not missing:
        return
    descriptions = {
        rm.key: rm.description
        for rm in state.required_metadata
        if rm.key in missing
    }
    detail = ", ".join(
        f"'{k}' ({descriptions[k]})" if descriptions[k] else f"'{k}'"
        for k in missing
    )
    raise TransitionError(
        f"State '{state.id}' requires metadata: {detail}. "
        f"Pass metadata={{...}} with the transition."
    )


# ---------------------------------------------------------------------------
# Signal planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalPlan:
    """A validated signal delivery, with an optional follow-on target."""

    current: ProcessState
    target: ProcessState | None


def plan_signal(
    defn: ProcessDefinition,
    instance: ProcessInstance,
    signal_name: str,
    data: dict[str, Any],
    target_id: str | None = None,
) -> SignalPlan:
    """Validate a signal delivery and plan its execution.

    Raises TransitionError for any illegal request. Performs no I/O.
    """
    if not instance.waiting:
        raise TransitionError(
            f"Instance '{instance.instance_id}' is not waiting for a signal"
        )

    current = defn.get_state(instance.current_state)
    if current is None or current.type != StateType.wait:
        raise TransitionError(
            f"Current state '{instance.current_state}' is not a wait state"
        )

    if current.signal.name != signal_name:
        raise TransitionError(
            f"Expected signal '{current.signal.name}', "
            f"got '{signal_name}'"
        )

    missing = [
        f.key for f in current.signal.required_fields if f.key not in data
    ]
    if missing:
        raise TransitionError(
            f"Signal missing required fields: {', '.join(missing)}"
        )

    target: ProcessState | None = None
    if target_id:
        if target_id not in current.transitions:
            raise TransitionError(
                f"'{target_id}' is not a valid transition from "
                f"wait state '{current.id}'. "
                f"Available: {current.transitions}"
            )
        target = defn.get_state(target_id)
        if target is not None and target.type == StateType.dispatch:
            if defn.get_state(target.immediate) is None:
                raise TransitionError(
                    f"Dispatch immediate target '{target.immediate}' "
                    f"not found in definition"
                )

    return SignalPlan(current=current, target=target)


# ---------------------------------------------------------------------------
# Child parameter resolution (subprocess and dispatch states)
# ---------------------------------------------------------------------------


def resolve_child_parameters(
    state: ProcessState,
    param_context: dict[str, str],
    child_defn: ProcessDefinition,
    kind: str = "Subprocess",
) -> dict[str, str]:
    """Resolve the parameters a child process receives from its parent.

    Substitutes the state's parameter_map templates against the given
    context, fails fast on unresolved placeholders, then applies the
    child definition's required/default rules. Raises SubprocessError.

    ``kind`` labels error messages ("Subprocess" or "Dispatch process").
    """
    child_params: dict[str, str] = {}
    if state.parameter_map:
        for child_key, template in state.parameter_map.items():
            substituted = substitute_params(template, param_context)
            unresolved = unresolved_placeholders(substituted)
            if unresolved:
                raise SubprocessError(
                    f"Dispatch to '{state.process}' has unresolved "
                    f"parameter(s) in parameter_map entry '{child_key}': "
                    f"{', '.join(unresolved)}. "
                    f"Available keys: {sorted(param_context.keys())}. "
                    f"Pass the missing value(s) via the transition "
                    f"metadata argument."
                )
            child_params[child_key] = substituted

    for p in child_defn.parameters:
        if p.required and p.name not in child_params:
            raise SubprocessError(
                f"{kind} '{state.process}' requires parameter "
                f"'{p.name}' but it is not in parameter_map"
            )
        if p.name not in child_params and p.default is not None:
            child_params[p.name] = p.default

    return child_params


def coerce_extras(mapping: dict[str, Any] | None) -> dict[str, str]:
    """Extract scalar values from metadata or signal data as strings,
    for merging into a child's parameter context."""
    return {
        k: str(v)
        for k, v in (mapping or {}).items()
        if isinstance(v, (str, int, float, bool))
    }


# ---------------------------------------------------------------------------
# Parent resumption (when a child completes or is abandoned)
# ---------------------------------------------------------------------------


def resolve_parent_transitions(
    parent_defn: ProcessDefinition,
    parent: ProcessInstance,
    outcome: str,
    child_terminal_state: str | None = None,
) -> list[str] | None:
    """Compute the transitions available to a parent when its child ends.

    Returns None if the parent's current state has no subprocess
    routing (nothing to resume against).
    """
    subprocess_state = parent_defn.get_state(parent.current_state)
    if subprocess_state is None or not subprocess_state.subprocess_routing:
        return None

    routing = subprocess_state.subprocess_routing
    if outcome == "completed" and child_terminal_state:
        return routing.resolve_on_complete(child_terminal_state)
    if outcome == "completed":
        return routing.on_complete_targets()
    return routing.on_fail


# ---------------------------------------------------------------------------
# Instance summaries
# ---------------------------------------------------------------------------


def summarize(instance: ProcessInstance) -> dict[str, Any]:
    """Compute a summary of a completed process instance."""
    states_visited: list[str] = []
    for h in instance.history:
        if not states_visited or states_visited[-1] != h.from_state:
            states_visited.append(h.from_state)
        states_visited.append(h.to_state)

    # Deduplicate while preserving order for the unique set
    unique_states = list(dict.fromkeys(states_visited))

    total_validations = 0
    passed_validations = 0
    failed_validations = 0
    for h in instance.history:
        for v in h.validations:
            total_validations += 1
            if v.get("passed"):
                passed_validations += 1
            else:
                failed_validations += 1

    try:
        started = datetime.fromisoformat(instance.started_at)
        ended = datetime.fromisoformat(instance.updated_at)
        elapsed = ended - started
        elapsed_str = str(elapsed).split(".")[0]  # drop microseconds
    except (ValueError, TypeError):
        elapsed_str = "unknown"

    return {
        "states_visited": unique_states,
        "transition_count": len(instance.history),
        "override_count": len(instance.overrides),
        "validations_run": total_validations,
        "validations_passed": passed_validations,
        "validations_failed": failed_validations,
        "elapsed": elapsed_str,
    }
