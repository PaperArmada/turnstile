"""Notification hooks triggered by process lifecycle events."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def run_notification(
    command_template: str,
    context: dict[str, str],
    cwd: Path,
    timeout: int = 30,
) -> dict[str, Any]:
    """Run a notification command with template substitution.

    Template variables use {name} syntax. Unknown variables are left as-is.

    Returns a result dict with success, output, and error info.
    """
    # Substitute known variables, leave unknown ones as-is
    command = command_template
    for key, value in context.items():
        command = command.replace(f"{{{key}}}", value)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        return {
            "success": proc.returncode == 0,
            "command": command,
            "exit_code": proc.returncode,
            "stdout": stdout.decode().strip() if stdout else "",
            "stderr": stderr.decode().strip() if stderr else "",
        }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "command": command,
            "error": f"Notification timed out after {timeout}s",
        }
    except Exception as e:
        return {
            "success": False,
            "command": command,
            "error": str(e),
        }


async def fire_notification(
    event: str,
    notifications: dict[str, str],
    context: dict[str, str],
    cwd: Path,
) -> dict[str, Any] | None:
    """Fire a notification for a lifecycle event if configured.

    Args:
        event: Event name (on_complete, on_override, on_abandon, etc.).
        notifications: Registry notifications config (event -> command template).
        context: Template variables for substitution.
        cwd: Working directory for command execution.

    Returns:
        Result dict if a notification was fired, None if no handler configured.
    """
    template = notifications.get(event)
    if not template:
        return None

    result = await run_notification(template, context, cwd)
    if not result["success"]:
        logger.warning(
            "Notification '%s' failed: %s",
            event,
            result.get("error") or result.get("stderr", ""),
        )
    return result
