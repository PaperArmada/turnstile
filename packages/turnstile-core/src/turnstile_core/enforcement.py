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

from turnstile_core.loader import (
    RegistryConfig,
    discover_definitions_full,
    load_registry,
)
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


def _unknown_state_result(
    mode: str, reason: str, guidance: str
) -> EnforcementResult:
    """Fail closed when enforcement state cannot be determined.

    Any inability to resolve the configuration or an active instance's
    definition means we cannot prove the action is permitted; treat it like a
    corrupt state file — deny in enforce, warn in monitor — rather than letting
    the error propagate to the guard's fail-open handler (SECURITY-NOTES F5).
    """
    decision = "warn" if mode == "monitor" else "deny"
    return EnforcementResult(decision=decision, reason=reason, guidance=guidance)


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
        state_map = {s.id: s for s in defn.states}
        state_obj = state_map.get(instance.current_state)
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
        state_map = {s.id: s for s in defn.states}
        state_obj = state_map.get(instance.current_state)
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


def _first_path_blocker(
    contexts: list[EnforcementContext],
    file_path: str,
    project_root: Path,
) -> EnforcementContext | None:
    """Return the first context that restricts edit_paths and does not match
    file_path, or None if every restricting context permits it.

    A context with no edit_paths imposes no path restriction. Under
    most-restrictive-wins across concurrent instances, a file is editable only
    if it satisfies every instance that declares edit_paths — a single
    permissive instance must not lift another's restriction (SECURITY-NOTES F1).

    Patterns are matched relative to project_root using fnmatch.
    """
    try:
        rel = str(Path(file_path).resolve().relative_to(project_root.resolve()))
    except ValueError:
        rel = file_path  # Already relative or outside project
    for ctx in contexts:
        if not ctx.permissions.edit_paths:
            continue  # No path restriction from this context
        if not any(fnmatch(rel, pattern) for pattern in ctx.permissions.edit_paths):
            return ctx
    return None


def _check_edit_paths(
    contexts: list[EnforcementContext],
    file_path: str,
    project_root: Path,
) -> bool:
    """True only if file_path is permitted by every restricting context."""
    return _first_path_blocker(contexts, file_path, project_root) is None


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
) -> EnforcementResult:
    """Check whether an action is permitted given active process state.

    This is the central enforcement function. Platform adapters call this
    and translate the result into their native hook/response format.

    Args:
        project_root: The project root directory.
        action: The action being attempted (currently: "edit").
        file_path: The file being acted on (for contextual suggestions).

    Returns:
        EnforcementResult with decision, reason, context, and guidance.
    """
    try:
        registry = load_registry(project_root)
    except Exception as exc:
        # The registry itself is unreadable, so we cannot even determine the
        # enforcement mode. We cannot prove enforcement is off, so fail closed
        # rather than let the guard swallow the error into an allow (F5).
        return EnforcementResult(
            decision="deny",
            reason=(
                "Cannot read .processes/registry.yaml; enforcement "
                f"configuration is unknown ({exc})"
            ),
            guidance="registry.yaml is corrupt or unreadable. Fix it, then retry.",
        )
    mode = registry.settings.enforcement

    if mode == "off":
        return EnforcementResult(decision="allow", reason="Enforcement is off")

    try:
        return _evaluate_active_enforcement(
            registry, mode, project_root, action, file_path
        )
    except Exception as exc:
        # Defense in depth: once enforcement is known to be on, any
        # unanticipated failure to evaluate it fails closed, never open. The
        # guard's outer handler would otherwise turn a raise into an allow —
        # the fail-open class F5 exists to prevent (SECURITY-NOTES F5).
        return _unknown_state_result(
            mode,
            f"Enforcement evaluation failed; state is unknown ({exc})",
            "Enforcement could not be evaluated. Inspect .processes/ and "
            ".process-state/, then retry.",
        )


def _evaluate_active_enforcement(
    registry: RegistryConfig,
    mode: str,
    project_root: Path,
    action: str,
    file_path: str,
) -> EnforcementResult:
    """Evaluate enforcement when the mode is on (monitor or enforce).

    Extracted so check_enforcement can wrap it in a fail-closed backstop: an
    unhandled exception here must never reach the guard's fail-open catch.
    """
    # Load active instances. Constructing the store touches the filesystem
    # (_ensure_dirs), so a .process-state path that exists as a non-directory
    # raises here; treat any failure to read state as unknown enforcement state
    # rather than letting it reach the guard's fail-open catch (SECURITY-NOTES F5).
    state_dir = project_root / registry.settings.state_dir
    try:
        store = StateStore(state_dir)
        active, corrupt = store.list_active_with_errors()
    except Exception as exc:
        return _unknown_state_result(
            mode,
            f"Cannot read process state under {state_dir}; enforcement "
            f"state is unknown ({exc})",
            "The .process-state directory is unreadable or is not a "
            "directory. Fix it, then retry.",
        )

    # A corrupt/unreadable state file means enforcement state is unknown.
    # Never treat that as "no restrictions" — that is the fail-open chain
    # where one torn file silently disables enforce mode (SECURITY-NOTES F5).
    if corrupt:
        names = ", ".join(p.name for p in corrupt)
        return _unknown_state_result(
            mode,
            f"Cannot read {len(corrupt)} process state file(s) "
            f"({names}); enforcement state is unknown",
            "A state file under .process-state/active/ is corrupt or "
            "unreadable. Inspect or remove it, then retry.",
        )

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

    # Load definitions to resolve state permissions. A definition that fails
    # to load (or a registry-listed local definition that is corrupt) leaves us
    # unable to resolve permissions; fail closed rather than default permissive.
    try:
        discovered = discover_definitions_full(project_root)
    except Exception as exc:
        return _unknown_state_result(
            mode,
            f"Cannot load process definitions; enforcement state is "
            f"unknown ({exc})",
            "A process definition is corrupt or unreadable. Fix it, then retry.",
        )
    definitions = {name: d.definition for name, d in discovered.items()}

    # An active instance whose definition cannot be resolved would otherwise
    # get the permissive default in _build_context, silently lifting a
    # restrictive state. Treat it as unknown enforcement state (F5, finding 2).
    unresolved = sorted(
        {inst.process_name for inst in active if inst.process_name not in definitions}
    )
    if unresolved:
        return _unknown_state_result(
            mode,
            f"Cannot resolve definition(s) for active instance(s): "
            f"{', '.join(unresolved)}; enforcement state is unknown",
            "An active instance's process definition is missing or "
            "unreadable. Restore the definition (or abandon the instance), "
            "then retry.",
        )

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

    # Most-restrictive-wins across concurrent instances: the action is
    # permitted only if EVERY non-suspended instance permits it. Otherwise a
    # second, more permissive instance could lift a restrictive state — a
    # verified bypass of per-state permissions (SECURITY-NOTES F1).
    denied_by = [
        c for c in non_suspended
        if not getattr(c.permissions, action, True)
    ]

    if denied_by:
        blocked_ctx = denied_by[0]
        edit_states = [t for t in blocked_ctx.available_transitions]
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

    # Every non-suspended instance permits the action. Enforce path
    # restrictions with the same rule: the file must satisfy every instance
    # that declares edit_paths.
    if file_path and action == "edit":
        blocker = _first_path_blocker(non_suspended, file_path, project_root)
        if blocker is not None:
            allowed_patterns = blocker.permissions.edit_paths
            reason = (
                f"File '{file_path}' is outside allowed edit paths "
                f"for {blocker.process_name} @ {blocker.current_state}: "
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

    # Catalogue mismatch: active process exists but doesn't match the
    # catalogue for the file being edited (advisory only).
    if file_path and registry.settings.path_catalogue:
        catalogue_procs = _match_catalogue(
            file_path, registry.settings.path_catalogue,
        )
        if catalogue_procs:
            active_names = {c.process_name for c in non_suspended}
            if not active_names & set(catalogue_procs):
                guidance += (
                    f"\n  Catalogue hint: '{file_path}' is typically "
                    f"covered by: {', '.join(catalogue_procs)}"
                )

    # Every non-suspended instance permits the action in its current state.
    allower = non_suspended[0]
    return EnforcementResult(
        decision="allow",
        reason=f"Permitted by {allower.process_name} @ {allower.current_state}",
        context=contexts,
        guidance=guidance,
    )
