"""Agent-agnostic enforcement logic for turnstile processes.

This module answers the question: "Given the current state of active
processes, is this action permitted?" Platform-specific adapters
(guard.py for Claude Code, git hooks, CI checks) call into this module
and translate the result into their native response format.

The enforcement check is deliberately lightweight: it reads persisted
state and process definitions but never modifies them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from turnstile_core.loader import load_registry, discover_definitions_full
from turnstile_core.models import ProcessDefinition, StatePermissions
from turnstile_core.persistence import ProcessInstance, StateStore


@dataclass
class EnforcementContext:
    """State context for a single active process instance."""

    instance_id: str
    process_name: str
    current_state: str
    state_description: str
    available_transitions: list[str]
    permissions: StatePermissions
    suspended: bool = False


@dataclass
class EnforcementResult:
    """Result of an enforcement check.

    Attributes:
        decision: "allow", "warn", or "deny"
        reason: Human-readable explanation of the decision.
        context: Active process instances with their state info.
        guidance: Actionable message for the agent (what to do next).
    """

    decision: str  # "allow" | "warn" | "deny"
    reason: str
    context: list[EnforcementContext] = field(default_factory=list)
    guidance: str = ""


def _build_context(
    instance: ProcessInstance,
    definitions: dict[str, ProcessDefinition],
) -> EnforcementContext:
    """Build enforcement context for a single active instance."""
    defn = definitions.get(instance.process_name)
    state_desc = ""
    transitions: list[str] = []
    permissions = StatePermissions()  # default: permissive

    if defn:
        state_map = {s.id: s for s in defn.states}
        state_obj = state_map.get(instance.current_state)
        if state_obj:
            state_desc = state_obj.description
            transitions = state_obj.transitions
            permissions = state_obj.permissions

    return EnforcementContext(
        instance_id=instance.instance_id,
        process_name=instance.process_name,
        current_state=instance.current_state,
        state_description=state_desc,
        available_transitions=transitions,
        permissions=permissions,
        suspended=instance.suspended,
    )


def _format_guidance(contexts: list[EnforcementContext], action: str) -> str:
    """Generate actionable guidance for the agent."""
    if not contexts:
        return ""

    lines = []
    for ctx in contexts:
        if ctx.suspended:
            continue
        lines.append(
            f"[{ctx.instance_id}] {ctx.process_name} @ {ctx.current_state}"
        )
        if ctx.state_description:
            lines.append(f"  {ctx.state_description}")
        if ctx.available_transitions:
            lines.append(
                f"  Next: {', '.join(ctx.available_transitions)}"
            )
    return "\n".join(lines)


def check_enforcement(
    project_root: Path,
    action: str = "edit",
) -> EnforcementResult:
    """Check whether an action is permitted given active process state.

    This is the central enforcement function. Platform adapters call this
    and translate the result into their native hook/response format.

    Args:
        project_root: The project root directory.
        action: The action being attempted (currently: "edit").

    Returns:
        EnforcementResult with decision, reason, context, and guidance.
    """
    registry = load_registry(project_root)
    mode = registry.settings.enforcement

    if mode == "off":
        return EnforcementResult(decision="allow", reason="Enforcement is off")

    # Load active instances
    state_dir = project_root / registry.settings.state_dir
    store = StateStore(state_dir)
    active = store.list_active()

    if not active:
        reason = f"No active turnstile process ({mode} mode)"
        guidance = (
            f"Start a process before making changes. "
            f"Use process_start() or check process_list() for options."
        )
        if mode == "monitor":
            return EnforcementResult(
                decision="warn", reason=reason, guidance=guidance
            )
        return EnforcementResult(
            decision="deny", reason=reason, guidance=guidance
        )

    # Load definitions to resolve state permissions
    discovered = discover_definitions_full(project_root)
    definitions = {name: d.definition for name, d in discovered.items()}

    contexts = [_build_context(inst, definitions) for inst in active]
    guidance = _format_guidance(contexts, action)

    # Check permissions: find any non-suspended instance that allows the action
    non_suspended = [c for c in contexts if not c.suspended]

    if not non_suspended:
        # All instances are suspended (waiting on subprocesses)
        reason = "All active processes are suspended (awaiting subprocesses)"
        if mode == "monitor":
            return EnforcementResult(
                decision="warn", reason=reason,
                context=contexts, guidance=guidance,
            )
        return EnforcementResult(
            decision="deny", reason=reason,
            context=contexts, guidance=guidance,
        )

    # Check the action permission on non-suspended instances
    permitted_by = [
        c for c in non_suspended
        if getattr(c.permissions, action, True)
    ]

    if permitted_by:
        # At least one active instance permits this action in its current state
        return EnforcementResult(
            decision="allow",
            reason=f"Permitted by {permitted_by[0].process_name} @ {permitted_by[0].current_state}",
            context=contexts,
            guidance=guidance,
        )

    # Action not permitted in any active instance's current state
    blocked_ctx = non_suspended[0]
    edit_states = [
        t for t in blocked_ctx.available_transitions
    ]
    suggestion = ""
    if edit_states:
        suggestion = (
            f" Transition to a state that allows edits first: "
            f"{', '.join(edit_states)}"
        )

    reason = (
        f"State '{blocked_ctx.current_state}' in {blocked_ctx.process_name} "
        f"does not allow {action}.{suggestion}"
    )

    if mode == "monitor":
        return EnforcementResult(
            decision="warn", reason=reason,
            context=contexts, guidance=guidance,
        )
    return EnforcementResult(
        decision="deny", reason=reason,
        context=contexts, guidance=guidance,
    )
