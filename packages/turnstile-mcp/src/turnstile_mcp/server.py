"""Turnstile MCP server: process enforcement tools for Claude Code."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from turnstile_core.engine import Engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("turnstile")

# Engine is initialized lazily on first tool call. The project root
# is determined from the CWD when the server starts.
_engine: Engine | None = None


def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        project_root = Path(os.getcwd())
        logger.info(f"Initializing turnstile engine at {project_root}")
        _engine = Engine(project_root)
    return _engine


# ---------------------------------------------------------------------------
# Process lifecycle tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def process_list(reload: bool = False) -> list[dict[str, Any]]:
    """List all available process definitions for this project.

    Args:
        reload: If true, reload definitions from disk before listing.
                Use after adding or editing YAML files.
    """
    engine = _get_engine()
    if reload:
        engine.reload()
    return engine.list_processes()


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
    instance_id: str, target_state: str
) -> dict[str, Any]:
    """Attempt to transition a process instance to a new state.

    Runs on_exit validations for the current state and on_enter
    validations for the target state. The transition is rejected if
    it is not a legal transition or if any validation with
    severity=error fails.

    Args:
        instance_id: The ID of the process instance.
        target_state: The state to transition to.
    """
    engine = _get_engine()
    result = await engine.transition(instance_id, target_state)
    return {
        "success": result.success,
        "new_state": result.new_state,
        "validation_results": result.validation_results,
        "available_transitions": result.available_transitions,
        "message": result.message,
    }


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
    result = await engine.skip(instance_id, target_state, reason)
    return {
        "success": result.success,
        "new_state": result.new_state,
        "available_transitions": result.available_transitions,
        "message": result.message,
    }


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


def main():
    """Entry point for the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
