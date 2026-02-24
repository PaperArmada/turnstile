"""Admin tools: graph generation, dry run, diffing."""

from __future__ import annotations

from typing import Any

from turnstile_core.models import (
    ProcessDefinition,
    StateType,
    ValidationRule,
    CompositeValidation,
)


# ---------------------------------------------------------------------------
# Mermaid graph generation
# ---------------------------------------------------------------------------


def generate_mermaid(defn: ProcessDefinition) -> dict[str, Any]:
    """Generate a Mermaid stateDiagram-v2 from a process definition."""
    lines = ["stateDiagram-v2"]

    # State descriptions and classifications
    for state in defn.states:
        sid = state.id
        label = state.description or sid

        if state.type == StateType.initial:
            lines.append(f"    [*] --> {sid}")
        if state.type == StateType.terminal:
            lines.append(f"    {sid} --> [*]")

        # Add state with description
        lines.append(f"    {sid} : {label}")

        # Mark states with validation gates
        has_enter = state.on_enter and state.on_enter.validations
        has_exit = state.on_exit and state.on_exit.validations
        if has_enter or has_exit:
            gate_markers = []
            if has_enter:
                gate_markers.append("entry gate")
            if has_exit:
                gate_markers.append("exit gate")
            lines.append(f"    {sid} : [{', '.join(gate_markers)}]")

    lines.append("")

    # Transitions
    for state in defn.states:
        for target in state.transitions:
            lines.append(f"    {state.id} --> {target}")

    mermaid_source = "\n".join(lines)

    return {
        "name": defn.name,
        "version": defn.version,
        "mermaid_source": mermaid_source,
        "state_count": len(defn.states),
        "transition_count": sum(len(s.transitions) for s in defn.states),
    }


# ---------------------------------------------------------------------------
# Dry run simulation
# ---------------------------------------------------------------------------


def _describe_validations(hooks) -> list[dict[str, Any]]:
    """Describe validations without executing them."""
    if not hooks or not hooks.validations:
        return []

    results = []
    for entry in hooks.validations:
        if isinstance(entry, ValidationRule):
            results.append({
                "type": "validation",
                "command": entry.command,
                "expect": entry.expect,
                "severity": entry.severity.value,
                "message": entry.message,
                "timeout": entry.timeout,
            })
        elif isinstance(entry, CompositeValidation):
            composite_type = "any" if entry.any_of else "all"
            rules = entry.any_of or entry.all_of or []
            results.append({
                "type": f"composite_{composite_type}",
                "message": entry.message,
                "rules": [
                    {
                        "command": r.command,
                        "expect": r.expect,
                        "severity": r.severity.value,
                        "message": r.message,
                    }
                    for r in rules
                ],
            })
    return results


def _describe_actions(hooks) -> list[dict[str, str]]:
    """Describe action hooks without executing them."""
    if not hooks or not hooks.actions:
        return []
    return [{"command": a.command} for a in hooks.actions]


def simulate_dry_run(
    defn: ProcessDefinition, path: list[str] | None = None
) -> list[dict[str, Any]]:
    """Walk through a process definition showing what would happen.

    If path is given, simulates that specific sequence of transitions.
    If path is None, describes every state in definition order.
    """
    if path is not None:
        return _simulate_path(defn, path)
    return _describe_all_states(defn)


def _describe_all_states(defn: ProcessDefinition) -> list[dict[str, Any]]:
    """Describe every state in the definition."""
    results = []
    for state in defn.states:
        results.append({
            "state": state.id,
            "type": state.type.value,
            "description": state.description,
            "transitions": state.transitions,
            "on_enter_validations": _describe_validations(state.on_enter),
            "on_enter_actions": _describe_actions(state.on_enter),
            "on_exit_validations": _describe_validations(state.on_exit),
            "on_exit_actions": _describe_actions(state.on_exit),
            "metadata": state.metadata,
        })
    return results


def _simulate_path(
    defn: ProcessDefinition, path: list[str]
) -> list[dict[str, Any]]:
    """Simulate walking a specific path through the process."""
    results = []
    current = defn.initial_state()

    for i, target_id in enumerate(path):
        step: dict[str, Any] = {
            "step": i + 1,
            "from_state": current.id,
            "to_state": target_id,
            "legal": target_id in current.transitions,
            "on_exit_validations": _describe_validations(current.on_exit),
            "on_exit_actions": _describe_actions(current.on_exit),
        }

        target = defn.get_state(target_id)
        if target is None:
            step["error"] = f"State '{target_id}' does not exist"
            step["on_enter_validations"] = []
            step["on_enter_actions"] = []
            results.append(step)
            break

        step["on_enter_validations"] = _describe_validations(target.on_enter)
        step["on_enter_actions"] = _describe_actions(target.on_enter)
        step["available_after"] = target.transitions

        results.append(step)
        current = target

    return results
