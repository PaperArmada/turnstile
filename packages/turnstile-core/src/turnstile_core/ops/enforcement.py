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
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from turnstile_core.definition.loader import load_registry, discover_definitions_full
from turnstile_core.definition.model import ProcessDefinition, StatePermissions
from turnstile_core.instance import ProcessInstance, StateStore


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
    waiting: bool = False
    waiting_for_signal: str = ""
    role: str = ""
    agent_context_summary: str = ""
    staleness_hint: str = ""


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
    role = ""
    agent_context_summary = ""

    if defn:
        state_obj = defn.get_state(instance.current_state)
        if state_obj:
            state_desc = state_obj.description
            transitions = state_obj.transitions
            permissions = state_obj.permissions
            role = state_obj.role
            if state_obj.agent_context:
                parts = []
                if state_obj.agent_context.guidance:
                    # Compact: first 3 lines of guidance
                    guide_lines = state_obj.agent_context.guidance.strip().splitlines()
                    parts.extend(guide_lines[:3])
                    if len(guide_lines) > 3:
                        parts.append("...")
                if state_obj.agent_context.reference_files:
                    parts.append(
                        "Reference: "
                        + ", ".join(state_obj.agent_context.reference_files)
                    )
                if state_obj.agent_context.tools:
                    parts.append(
                        "Tools: "
                        + ", ".join(state_obj.agent_context.tools)
                    )
                agent_context_summary = "\n".join(parts)

    waiting_for = ""
    if instance.waiting and defn:
        state_obj = defn.get_state(instance.current_state)
        if state_obj and state_obj.signal:
            waiting_for = state_obj.signal.name

    return EnforcementContext(
        instance_id=instance.instance_id,
        process_name=instance.process_name,
        current_state=instance.current_state,
        state_description=state_desc,
        available_transitions=transitions,
        permissions=permissions,
        suspended=instance.suspended,
        waiting=instance.waiting,
        waiting_for_signal=waiting_for,
        role=role,
        agent_context_summary=agent_context_summary,
        staleness_hint=_staleness_hint(instance.updated_at),
    )


def _staleness_hint(updated_at: str) -> str:
    """Return a staleness warning if the instance hasn't been updated recently."""
    try:
        updated = datetime.fromisoformat(updated_at)
        now = datetime.now(timezone.utc)
        age = now - updated
        hours = age.total_seconds() / 3600
        if hours >= 24:
            days = int(hours // 24)
            return f"  STALE: last transition {days}d ago. Verify this is still your active work."
        if hours >= 4:
            return f"  Note: last transition {int(hours)}h ago."
    except (ValueError, TypeError):
        pass
    return ""


def _format_guidance(contexts: list[EnforcementContext], action: str) -> str:
    """Generate assertive state guidance for the agent.

    Uses declarative framing ('You are in: ...') rather than passive
    status lines so the output overrides stale mental models after
    compaction or session restart.
    """
    if not contexts:
        return ""

    non_suspended = [c for c in contexts if not c.suspended]
    suspended = [c for c in contexts if c.suspended]

    lines = []
    for ctx in non_suspended:
        lines.append(
            f"You are in: {ctx.process_name} @ {ctx.current_state} "
            f"(instance {ctx.instance_id})"
        )
        if ctx.state_description:
            lines.append(f"  {ctx.state_description}")
        if ctx.available_transitions:
            lines.append(
                f"  Next: {', '.join(ctx.available_transitions)}"
            )
        if ctx.role:
            lines.append(f"  Role: {ctx.role}")
        if ctx.staleness_hint:
            lines.append(ctx.staleness_hint)

    waiting = [c for c in contexts if c.waiting]
    if waiting:
        for ctx in waiting:
            lines.append(
                f"Waiting: {ctx.process_name} @ {ctx.current_state} "
                f"(instance {ctx.instance_id}, waiting for signal "
                f"'{ctx.waiting_for_signal}')"
            )

    if suspended:
        for ctx in suspended:
            lines.append(
                f"Suspended: {ctx.process_name} @ {ctx.current_state} "
                f"(instance {ctx.instance_id}, waiting on subprocess)"
            )

    if len(non_suspended) == 0 and len(suspended) > 0:
        lines.append("No active (non-suspended) processes.")

    return "\n".join(lines)


def _action_allowed(permissions: StatePermissions, action: str) -> bool:
    """Whether a state's permissions allow an action category at all."""
    if action == "edit":
        return permissions.edit
    if action == "run":
        return permissions.run
    return True


def _command_permitted(permissions: StatePermissions, command: str) -> str:
    """Check a command against a context's allow/deny patterns.

    Returns "" if permitted, or the reason it is blocked. Patterns are
    fnmatch globs tested against the full command string.
    """
    for pattern in permissions.deny_commands:
        if fnmatch(command, pattern):
            return f"matches denied pattern '{pattern}'"
    if permissions.allow_commands and not any(
        fnmatch(command, pattern) for pattern in permissions.allow_commands
    ):
        return (
            f"not in allowed patterns: "
            f"{', '.join(permissions.allow_commands)}"
        )
    return ""


def _check_command(
    contexts: list[EnforcementContext], command: str
) -> tuple[bool, str]:
    """Check a command against every permitting context.

    Permitted if at least one context's patterns allow it (mirroring
    _check_edit_paths). Returns (ok, blocking_reason).
    """
    reasons = []
    for ctx in contexts:
        blocked = _command_permitted(ctx.permissions, command)
        if not blocked:
            return True, ""
        reasons.append(
            f"{ctx.process_name} @ {ctx.current_state}: {blocked}"
        )
    return False, "; ".join(reasons)


def _check_edit_paths(
    contexts: list[EnforcementContext],
    file_path: str,
    project_root: Path,
) -> bool:
    """Check if file_path matches edit_paths for any permitting context.

    Returns True if:
    - No context has edit_paths restrictions (empty list), or
    - The file path matches at least one pattern in at least one context.

    Patterns are matched relative to project_root using fnmatch.
    """
    for ctx in contexts:
        if not ctx.permissions.edit_paths:
            return True  # No restrictions on this context
        # Make file_path relative to project root for matching
        try:
            rel = str(Path(file_path).resolve().relative_to(project_root.resolve()))
        except ValueError:
            rel = file_path  # Already relative or outside project
        for pattern in ctx.permissions.edit_paths:
            if fnmatch(rel, pattern):
                return True
    return False


def _suggest_processes(
    project_root: Path,
    file_path: str = "",
    path_catalogue: dict[str, list[str]] | None = None,
) -> str:
    """Build a process suggestion list from available definitions.

    When path_catalogue is provided and file_path matches a catalogue
    entry, narrows suggestions to the mapped processes. Otherwise
    lists all available processes.

    Returns a formatted string listing available processes with descriptions,
    suitable for inclusion in enforcement guidance.
    """
    try:
        discovered = discover_definitions_full(project_root)
    except Exception:
        return "Use process_start() or check process_list() for options."

    if not discovered:
        return "No process definitions found. Run 'turnstile init' first."

    # Narrow to catalogue matches if possible
    catalogue_matches = _match_catalogue(file_path, path_catalogue) if file_path and path_catalogue else []
    show_names = set(catalogue_matches) if catalogue_matches else set(discovered)

    lines = []
    if catalogue_matches:
        lines.append(f"Suggested processes for '{file_path}':")
    else:
        lines.append("Available processes:")

    for name in sorted(show_names):
        if name not in discovered:
            continue
        defn = discovered[name].definition
        desc = defn.description or ""
        params = [p.name for p in defn.parameters if p.required]
        param_hint = ""
        if params:
            param_hint = f" (requires: {', '.join(params)})"
        lines.append(f"  - {name}: {desc}{param_hint}")

    lines.append("")
    lines.append(
        'Start one with process_start("<name>", {<parameters>}) '
        "before editing files."
    )

    return "\n".join(lines)


def _match_catalogue(
    file_path: str,
    catalogue: dict[str, list[str]],
) -> list[str]:
    """Return process names from catalogue whose glob patterns match file_path.

    Catalogue keys are glob patterns (e.g. "src/**", "docs/*").
    Values are lists of process names that cover that directory.
    """
    matched: list[str] = []
    for pattern, processes in catalogue.items():
        if fnmatch(file_path, pattern):
            matched.extend(p for p in processes if p not in matched)
    return matched


def check_enforcement(
    project_root: Path,
    action: str = "edit",
    file_path: str = "",
    command: str = "",
) -> EnforcementResult:
    """Check whether an action is permitted given active process state.

    This is the central enforcement function. Platform adapters call this
    and translate the result into their native hook/response format.

    Args:
        project_root: The project root directory.
        action: The action being attempted ("edit" or "run").
        file_path: The file being acted on (edit; contextual suggestions).
        command: The shell command being attempted (run).

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
        guidance = _suggest_processes(
            project_root, file_path,
            path_catalogue=registry.settings.path_catalogue or None,
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

    # Check the action permission on non-suspended instances.
    # "edit" and "run" are governed; unknown actions are allowed, but
    # explicitly, not via a fail-open attribute lookup.
    permitted_by = [
        c for c in non_suspended
        if _action_allowed(c.permissions, action)
    ]

    if permitted_by:
        # Check command restrictions if a command is provided
        if command and action == "run":
            cmd_ok, blocked_reason = _check_command(permitted_by, command)
            if not cmd_ok:
                reason = f"Command blocked: {blocked_reason}"
                if mode == "monitor":
                    return EnforcementResult(
                        decision="warn", reason=reason,
                        context=contexts, guidance=guidance,
                    )
                return EnforcementResult(
                    decision="deny", reason=reason,
                    context=contexts, guidance=guidance,
                )

        # Check path restrictions if file_path is provided
        if file_path and action == "edit":
            path_ok = _check_edit_paths(permitted_by, file_path, project_root)
            if not path_ok:
                allowed_patterns = permitted_by[0].permissions.edit_paths
                reason = (
                    f"File '{file_path}' is outside allowed edit paths "
                    f"for {permitted_by[0].process_name} @ "
                    f"{permitted_by[0].current_state}: "
                    f"{', '.join(allowed_patterns)}"
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

        # Check catalogue mismatch: active process exists but doesn't
        # match the catalogue for the file being edited (advisory only)
        if file_path and registry.settings.path_catalogue:
            catalogue_procs = _match_catalogue(
                file_path, registry.settings.path_catalogue,
            )
            if catalogue_procs:
                active_names = {c.process_name for c in permitted_by}
                if not active_names & set(catalogue_procs):
                    guidance += (
                        f"\n  Catalogue hint: '{file_path}' is typically "
                        f"covered by: {', '.join(catalogue_procs)}"
                    )

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
