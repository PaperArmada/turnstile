"""Notification hooks triggered by process lifecycle events."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


async def run_notification(
    command_template: str,
    context: dict[str, str],
    cwd: Path,
    timeout: int = 30,
) -> dict[str, Any]:
    """Run a notification command with context supplied via the environment.

    Template variables use ${name} shell syntax and are expanded by the shell
    from environment variables, not interpolated into the command text. This
    prevents a context value (some of which, such as the skip reason and the
    acting user, are caller-supplied) from injecting shell commands. It is the
    same mechanism used for validation gates; see validator.run_command.

    Returns a result dict with success, output, and error info.
    """
    command = command_template
    env = os.environ.copy()
    for key, value in context.items():
        if _ENV_NAME_RE.match(key):
            env[key] = value

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
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
