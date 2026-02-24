"""Enforcement guard for Claude Code PreToolUse hooks.

When enforcement is enabled, this module checks whether an active turnstile
process exists before allowing file mutations (Write, Edit). It reads the
enforcement mode from registry.yaml and checks .process-state/active/ for
running instances.

Invoked as a Claude Code PreToolUse hook via: turnstile guard
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from turnstile_core.loader import load_registry
from turnstile_core.persistence import StateStore


def _find_turnstile_root() -> str:
    """Find the turnstile repo root by walking up from this file.

    Looks for the nearest ancestor containing both pyproject.toml
    and a packages/ directory (the workspace layout).
    """
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "pyproject.toml").exists() and (current / "packages").is_dir():
            return str(current)
        current = current.parent
    # Fallback: if running via uv --directory, CWD is the turnstile repo
    return str(Path.cwd())


def check_enforcement(project_root: Path) -> dict[str, Any]:
    """Check enforcement status and return a hook response.

    Returns a dict with:
    - action: "allow" | "deny" | "warn"
    - reason: human-readable explanation
    - response: dict to output as JSON for Claude Code hooks, or None
    """
    registry = load_registry(project_root)
    mode = registry.settings.enforcement

    if mode == "off":
        return {
            "action": "allow",
            "reason": "Enforcement is off",
            "response": None,
        }

    # Check for active instances
    state_dir = project_root / registry.settings.state_dir
    store = StateStore(state_dir)
    active = store.list_active()
    has_active = len(active) > 0

    if has_active:
        process_names = ", ".join(sorted({i.process_name for i in active}))
        return {
            "action": "allow",
            "reason": f"Active process(es): {process_names}",
            "response": None,
        }

    # No active process
    if mode == "monitor":
        return {
            "action": "warn",
            "reason": "No active turnstile process (monitor mode)",
            "response": {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": (
                        f"WARNING: No active turnstile process "
                        f"(checked {project_root}). "
                        f"Start a process with process_start before "
                        f"making changes."
                    ),
                }
            },
        }

    # enforce mode
    return {
        "action": "deny",
        "reason": "No active turnstile process (enforce mode)",
        "response": {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Turnstile enforcement is ON. No active process "
                    f"instance found in {project_root}. Start a process "
                    f"with process_start before making file changes."
                ),
            }
        },
    }


def run_guard() -> None:
    """Entry point for the Claude Code PreToolUse hook.

    Reads hook JSON from stdin, determines the project root from the
    cwd field, checks enforcement, and outputs the appropriate response.
    Fails open (allows) on any error to avoid blocking the agent.
    """
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw)
        cwd = hook_input.get("cwd", "")
        if not cwd:
            # No cwd in input, allow
            return

        project_root = Path(cwd)
        result = check_enforcement(project_root)

        if result["response"] is not None:
            print(json.dumps(result["response"]))

    except Exception:
        # Fail open: if anything goes wrong, allow the action
        pass


def _guard_command_uvx(repo_url: str) -> str:
    """Build the guard command using uvx (no local clone needed)."""
    return (
        f'uvx --python 3.12 --from "turnstile-cli @ '
        f'git+{repo_url}#subdirectory=packages/turnstile-cli" '
        f'turnstile guard'
    )


def _guard_command_dev(turnstile_dir: str) -> str:
    """Build the guard command using uv --directory (for development)."""
    return (
        f"uv --directory {turnstile_dir} "
        f"run --package turnstile-cli "
        f"turnstile guard"
    )


def generate_hook_config(
    turnstile_dir: str | None = None,
    repo_url: str | None = None,
) -> dict[str, Any]:
    """Generate the Claude Code hook configuration.

    Provide repo_url for uvx mode (default), or turnstile_dir for dev mode.

    Args:
        turnstile_dir: Absolute path to the turnstile repo (dev mode).
        repo_url: Git URL for the turnstile repo (uvx mode).

    Returns:
        The hooks configuration dict for .claude/settings.json.
    """
    if turnstile_dir:
        command = _guard_command_dev(turnstile_dir)
    elif repo_url:
        command = _guard_command_uvx(repo_url)
    else:
        raise ValueError("Either turnstile_dir or repo_url must be provided")

    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ],
                }
            ]
        }
    }


def install_enforcement(
    project_root: Path,
    mode: str,
    turnstile_dir: str | None = None,
    repo_url: str | None = None,
) -> dict[str, Any]:
    """Set up enforcement for a project.

    Updates .claude/settings.json with the PreToolUse hook configuration.
    Does NOT modify registry.yaml (the user or a separate command handles that).

    Provide repo_url for uvx mode (default) or turnstile_dir for dev mode.

    Args:
        project_root: The project root directory.
        mode: Enforcement mode ("enforce", "monitor", or "off").
        turnstile_dir: Path to the turnstile repo (dev mode).
        repo_url: Git URL for the turnstile repo (uvx mode).

    Returns:
        A result dict with status information.
    """
    if turnstile_dir is None and repo_url is None:
        turnstile_dir = _find_turnstile_root()

    settings_dir = project_root / ".claude"
    settings_path = settings_dir / "settings.json"

    if mode == "off":
        # Remove hook from settings if present
        if settings_path.exists():
            settings = json.loads(settings_path.read_text())
            hooks = settings.get("hooks", {})
            pre_tool = hooks.get("PreToolUse", [])
            # Remove turnstile entries
            pre_tool = [
                h for h in pre_tool
                if not any(
                    "turnstile guard" in hk.get("command", "")
                    for hk in h.get("hooks", [])
                )
            ]
            if pre_tool:
                hooks["PreToolUse"] = pre_tool
            else:
                hooks.pop("PreToolUse", None)
            if hooks:
                settings["hooks"] = hooks
            else:
                settings.pop("hooks", None)
            settings_path.write_text(json.dumps(settings, indent=2) + "\n")

        return {
            "installed": True,
            "mode": "off",
            "message": "Enforcement hook removed from .claude/settings.json",
        }

    # Install hook for enforce or monitor mode
    hook_config = generate_hook_config(
        turnstile_dir=turnstile_dir, repo_url=repo_url
    )

    settings_dir.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
    else:
        settings = {}

    # Merge hook config: replace any existing turnstile PreToolUse entries
    existing_hooks = settings.get("hooks", {})
    existing_pre_tool = existing_hooks.get("PreToolUse", [])

    # Remove old turnstile entries
    existing_pre_tool = [
        h for h in existing_pre_tool
        if not any(
            "turnstile guard" in hk.get("command", "")
            for hk in h.get("hooks", [])
        )
    ]

    # Add new turnstile entry
    existing_pre_tool.extend(hook_config["hooks"]["PreToolUse"])

    existing_hooks["PreToolUse"] = existing_pre_tool
    settings["hooks"] = existing_hooks

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    return {
        "installed": True,
        "mode": mode,
        "settings_path": str(settings_path),
        "message": f"Enforcement hook installed ({mode} mode)",
    }


def update_registry_enforcement(project_root: Path, mode: str) -> bool:
    """Update the enforcement field in registry.yaml.

    Creates the file with minimal content if it doesn't exist.
    Returns True if the file was updated.
    """
    import yaml

    registry_path = project_root / ".processes" / "registry.yaml"

    if registry_path.exists():
        raw = yaml.safe_load(registry_path.read_text()) or {}
    else:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {"version": "1.0"}

    settings = raw.get("settings", {})
    settings["enforcement"] = mode
    raw["settings"] = settings

    registry_path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
    return True
