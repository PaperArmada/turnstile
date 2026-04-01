"""Turnstile MCP server: process enforcement tools for Claude Code."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from turnstile_core.engine import Engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Session-scoped identifier: generated once when the MCP server starts,
# shared by all tool calls within this session. Used for correlating
# transitions made in the same conversation and for concurrency detection.
_session_id: str = uuid.uuid4().hex[:12]

mcp = FastMCP("turnstile")

# Engine is initialized lazily on first tool call. The project root
# is determined from TURNSTILE_PROJECT_DIR (if set) or CWD.
# TURNSTILE_PROJECT_DIR is needed when `uv --directory` overrides CWD
# to the turnstile source repo rather than the consumer project.
_engine: Engine | None = None


def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        project_dir = os.environ.get("TURNSTILE_PROJECT_DIR")
        if project_dir:
            project_root = Path(project_dir)
        else:
            project_root = Path(os.getcwd())
        logger.info(f"Initializing turnstile engine at {project_root}")
        _engine = Engine(project_root)
    return _engine


# ---------------------------------------------------------------------------
# Process lifecycle tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def process_list(reload: bool = False) -> dict[str, Any]:
    """List all available process definitions for this project.

    Args:
        reload: If true, reload definitions from disk before listing.
                Use after adding or editing YAML files.
    """
    engine = _get_engine()
    if reload:
        engine.reload()
    return {
        "project_root": str(engine.project_root),
        "processes": engine.list_processes(),
    }


@mcp.tool()
async def process_reload_definitions() -> dict[str, Any]:
    """Reload all process definitions from disk.

    Call this after editing YAML files in .processes/ to pick up
    changes without restarting the MCP server. Reloads both the
    registry and all process definitions.
    """
    engine = _get_engine()
    engine.reload()
    return {
        "reloaded": True,
        "project_root": str(engine.project_root),
        "processes": engine.list_processes(),
    }


@mcp.tool()
async def process_info(name: str) -> dict[str, Any]:
    """Get detailed information about a process definition.

    Shows parameters (with descriptions, defaults, and whether they're
    required), states (with descriptions, permissions, and transitions),
    and metadata. Use this to discover required parameters before calling
    process_start.

    Args:
        name: The name of the process definition.
    """
    engine = _get_engine()
    return engine.info(name)


@mcp.tool()
async def process_start(
    name: str, parameters: dict[str, str] | None = None
) -> dict[str, Any]:
    """Start a new instance of a named process.

    Args:
        name: The name of the process definition to start.
        parameters: Optional key-value parameters for the process instance.
    """
    engine = _get_engine()
    return engine.start(name, parameters)


@mcp.tool()
async def process_status(
    instance_id: str | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Get status of active process instance(s).

    If no instance_id is provided, returns all active instances.

    Args:
        instance_id: Optional ID of a specific process instance.
    """
    engine = _get_engine()
    return engine.status(instance_id)


@mcp.tool()
async def process_transition(
    instance_id: str, target_state: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attempt to transition a process instance to a new state.

    Runs on_exit validations for the current state and on_enter
    validations for the target state. The transition is rejected if
    it is not a legal transition or if any validation with
    severity=error fails.

    Args:
        instance_id: The ID of the process instance.
        target_state: The state to transition to.
        metadata: Optional structured context for this transition
                  (e.g. {"cluster_id": "valuation", "iteration": "2"}).
                  Stored in history for analytics and audit.
    """
    engine = _get_engine()
    result = await engine.transition(
        instance_id, target_state, metadata=metadata, session_id=_session_id,
    )
    response: dict[str, Any] = {
        "success": result.success,
        "new_state": result.new_state,
        "validation_results": result.validation_results,
        "available_transitions": result.available_transitions,
        "message": result.message,
    }
    if result.role:
        response["role"] = result.role
    if result.agent_context:
        response["agent_context"] = result.agent_context
    if result.subprocess_started:
        response["subprocess_started"] = result.subprocess_started
    if result.parent_resumed:
        response["parent_resumed"] = True
        response["parent_instance_id"] = result.parent_instance_id
        response["parent_available_transitions"] = result.parent_available_transitions
    if result.skill_directives:
        response["skill_directives"] = result.skill_directives
    if result.required_metadata:
        response["required_metadata"] = result.required_metadata
    if result.summary:
        response["summary"] = result.summary
    return response


@mcp.tool()
async def process_skip(
    instance_id: str, target_state: str, reason: str
) -> dict[str, Any]:
    """Force-skip to a state, bypassing normal transition rules.

    This is an override that logs the reason. Use when a step must
    be bypassed due to urgency or special circumstances.

    Args:
        instance_id: The ID of the process instance.
        target_state: The state to skip to.
        reason: Why this override is necessary (logged for audit).
    """
    engine = _get_engine()
    result = await engine.skip(instance_id, target_state, reason, session_id=_session_id)
    return {
        "success": result.success,
        "new_state": result.new_state,
        "available_transitions": result.available_transitions,
        "message": result.message,
    }


@mcp.tool()
async def process_signal(
    instance_id: str, signal_name: str,
    data: dict[str, Any],
    target_state: str | None = None,
) -> dict[str, Any]:
    """Deliver a signal to a waiting process instance.

    Wait states block until an external signal arrives. This tool
    delivers the signal, validates required fields, and optionally
    transitions the instance to a target state in one step.

    Args:
        instance_id: The ID of the waiting process instance.
        signal_name: Must match the wait state's expected signal name.
        data: Signal payload (key-value pairs matching required_fields).
        target_state: Optional state to transition to immediately.
                      Must be in the wait state's transitions list.
    """
    engine = _get_engine()
    return engine.receive_signal(
        instance_id, signal_name, data,
        target_state=target_state, session_id=_session_id,
    )


@mcp.tool()
async def process_abandon(
    instance_id: str, reason: str
) -> dict[str, Any]:
    """Abandon a process instance.

    Marks the instance as abandoned and moves it out of the active list.

    Args:
        instance_id: The ID of the process instance.
        reason: Why the process is being abandoned.
    """
    engine = _get_engine()
    return engine.abandon(instance_id, reason)


@mcp.tool()
async def process_undo(
    instance_id: str, reason: str
) -> dict[str, Any]:
    """Revert the last transition (administrative correction).

    Moves the process back one state with no validations. Cannot undo
    past a process_skip.

    Args:
        instance_id: The ID of the process instance.
        reason: Why this undo is necessary.
    """
    engine = _get_engine()
    result = engine.undo(instance_id, reason)
    return {
        "success": result.success,
        "new_state": result.new_state,
        "available_transitions": result.available_transitions,
        "message": result.message,
    }


@mcp.tool()
async def process_handoff(
    instance_id: str, to_user: str, reason: str
) -> dict[str, Any]:
    """Log an explicit ownership transfer for audit purposes.

    Does not enforce access control; purely metadata.

    Args:
        instance_id: The ID of the process instance.
        to_user: The user receiving ownership.
        reason: Why the handoff is happening.
    """
    engine = _get_engine()
    return engine.handoff(instance_id, to_user, reason)


@mcp.tool()
async def process_history(instance_id: str) -> list[dict[str, Any]]:
    """Get full transition history for a process instance.

    Args:
        instance_id: The ID of the process instance.
    """
    engine = _get_engine()
    return engine.history(instance_id)


@mcp.tool()
async def process_validate_definition(path: str) -> dict[str, Any]:
    """Validate a process definition YAML file against the schema.

    Checks valid YAML, valid schema, all transitions reference real
    states, initial/terminal states exist, etc.

    Args:
        path: Path to the YAML process definition file.
    """
    engine = _get_engine()
    return engine.validate_definition(path)


@mcp.tool()
async def process_graph(name: str) -> dict[str, Any]:
    """Generate a Mermaid state diagram for a process definition.

    Returns Mermaid source that can be rendered in any Mermaid viewer,
    GitHub markdown, or VS Code preview.

    Args:
        name: The name of the process definition.
    """
    engine = _get_engine()
    return engine.graph(name)


@mcp.tool()
async def process_dry_run(
    name: str, path: list[str] | None = None
) -> list[dict[str, Any]]:
    """Simulate a process execution without running any commands.

    Shows what validations would run at each gate and what transitions
    are available. No state changes, no shell commands executed.

    If path is provided, simulates that specific sequence of transitions.
    Otherwise, describes every state in the definition.

    Args:
        name: The name of the process definition.
        path: Optional list of state IDs to simulate walking through.
    """
    engine = _get_engine()
    return engine.dry_run(name, path)


@mcp.tool()
async def process_diff(path_a: str, path_b: str) -> dict[str, Any]:
    """Compare two process definition YAML files.

    Shows added/removed/modified states, transition changes, and
    parameter changes. Useful for reviewing changes to definitions.

    Args:
        path_a: Path to the first (older) YAML definition file.
        path_b: Path to the second (newer) YAML definition file.
    """
    engine = _get_engine()
    return engine.diff(path_a, path_b)


@mcp.tool()
async def process_migrate(instance_id: str) -> dict[str, Any]:
    """Check if an in-flight process instance needs migration.

    Detects when the process definition has changed since the instance
    was started, and reports whether the instance can continue with
    the new definition or needs intervention.

    Args:
        instance_id: The ID of the process instance to check.
    """
    engine = _get_engine()
    return engine.migrate(instance_id)


@mcp.tool()
async def process_analytics() -> dict[str, Any]:
    """Compute process analytics from archived (completed/abandoned) instances.

    Returns per-process statistics including completion rates,
    average durations per state, and override patterns.
    """
    engine = _get_engine()
    return engine.analytics()


@mcp.tool()
async def process_check_completed(
    name: str,
    state: str | None = None,
    parameters: dict[str, str] | None = None,
    include_active: bool = False,
    parent: str | None = None,
) -> dict[str, Any]:
    """Check whether a completed process instance matches criteria.

    Used for CI checks and git hooks. Returns whether a matching
    instance exists. Searches completed instances by default.

    Args:
        name: The process definition name to search for.
        state: Optional state that must have been reached (in history).
        parameters: Optional parameter key-value filters.
        include_active: Also search active (in-progress) instances.
        parent: Optional parent instance ID filter (for dispatched children).
    """
    engine = _get_engine()
    return engine.check_completed(
        name, state, parameters, include_active,
        parent_instance_id=parent,
    )


def main():
    """Entry point for the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
