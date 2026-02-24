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


# ---------------------------------------------------------------------------
# Definition diffing
# ---------------------------------------------------------------------------


def diff_definitions(
    defn_a: ProcessDefinition, defn_b: ProcessDefinition
) -> dict[str, Any]:
    """Compare two process definitions and return their differences.

    Compares states, transitions, validations, and parameters.
    """
    states_a = {s.id: s for s in defn_a.states}
    states_b = {s.id: s for s in defn_b.states}
    ids_a = set(states_a.keys())
    ids_b = set(states_b.keys())

    added = sorted(ids_b - ids_a)
    removed = sorted(ids_a - ids_b)
    common = sorted(ids_a & ids_b)

    modified: list[dict[str, Any]] = []
    transition_changes: list[dict[str, Any]] = []

    for sid in common:
        sa, sb = states_a[sid], states_b[sid]
        changes: dict[str, Any] = {"state": sid}
        has_changes = False

        # Type change
        if sa.type != sb.type:
            changes["type"] = {"from": sa.type.value, "to": sb.type.value}
            has_changes = True

        # Description change
        if sa.description != sb.description:
            changes["description"] = {"from": sa.description, "to": sb.description}
            has_changes = True

        # Transition changes
        trans_a = set(sa.transitions)
        trans_b = set(sb.transitions)
        if trans_a != trans_b:
            tc = {
                "state": sid,
                "added": sorted(trans_b - trans_a),
                "removed": sorted(trans_a - trans_b),
            }
            transition_changes.append(tc)
            has_changes = True

        # Validation changes (compare by serialization)
        enter_a = _serialize_hooks(sa.on_enter)
        enter_b = _serialize_hooks(sb.on_enter)
        if enter_a != enter_b:
            changes["on_enter_changed"] = True
            has_changes = True

        exit_a = _serialize_hooks(sa.on_exit)
        exit_b = _serialize_hooks(sb.on_exit)
        if exit_a != exit_b:
            changes["on_exit_changed"] = True
            has_changes = True

        if has_changes:
            modified.append(changes)

    # Parameter changes
    params_a = {p.name: p for p in defn_a.parameters}
    params_b = {p.name: p for p in defn_b.parameters}
    param_names_a = set(params_a.keys())
    param_names_b = set(params_b.keys())

    param_changes: dict[str, Any] = {}
    if param_names_a != param_names_b or any(
        params_a[n].model_dump() != params_b[n].model_dump()
        for n in param_names_a & param_names_b
    ):
        param_changes = {
            "added": sorted(param_names_b - param_names_a),
            "removed": sorted(param_names_a - param_names_b),
        }

    return {
        "name": defn_b.name,
        "version_a": defn_a.version,
        "version_b": defn_b.version,
        "added_states": added,
        "removed_states": removed,
        "modified_states": modified,
        "transition_changes": transition_changes,
        "parameter_changes": param_changes,
        "summary": _diff_summary(added, removed, modified, transition_changes),
    }


def _serialize_hooks(hooks) -> str:
    """Serialize hooks to a comparable string."""
    if hooks is None:
        return ""
    return hooks.model_dump_json(by_alias=True)


def _diff_summary(
    added: list[str],
    removed: list[str],
    modified: list[dict],
    transition_changes: list[dict],
) -> str:
    """Generate a human-readable summary of changes."""
    parts = []
    if added:
        parts.append(f"{len(added)} state(s) added")
    if removed:
        parts.append(f"{len(removed)} state(s) removed")
    if modified:
        parts.append(f"{len(modified)} state(s) modified")
    if transition_changes:
        parts.append(f"{len(transition_changes)} transition change(s)")
    return "; ".join(parts) if parts else "no changes"
