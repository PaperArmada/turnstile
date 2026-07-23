"""Claude Code adapter for turnstile enforcement.

This is the platform-specific edge for Claude Code: it reads the CC
PreToolUse hook JSON from stdin, delegates to the agent-agnostic
enforcement contract in turnstile-core (`enforcement.check_enforcement`
returning an `EnforcementResult`), and translates the result into CC's
hook response shapes. It also owns installing/removing the hook in
`.claude/settings.json` and building the pinned guard commands.

turnstile-core knows nothing about Claude Code hook formats; any other
runtime adapter should consume the same core contract this one does.

Invoked as a Claude Code PreToolUse hook via: turnstile guard
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from turnstile_core.enforcement import EnforcementResult, check_enforcement
from turnstile_core.persistence import _atomic_write_text


def _load_settings(settings_path: Path) -> dict[str, Any]:
    """Parse .claude/settings.json, failing with an actionable message.

    A corrupt settings file must produce a clear error naming the file,
    not a traceback, and must never be silently overwritten: it can
    carry non-turnstile configuration the user would lose.
    """
    try:
        return json.loads(settings_path.read_text())
    except (OSError, ValueError) as e:
        raise ValueError(
            f"Cannot read {settings_path}: {e}. Fix or remove the file, "
            f"then re-run; turnstile will not overwrite it."
        ) from e


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


def _to_hook_response(result: EnforcementResult, project_root: Path) -> dict[str, Any] | None:
    """Translate an EnforcementResult into a Claude Code hook response.

    Returns None if no hook output is needed (silent allow).
    """
    if result.decision == "allow" and not result.context:
        # Enforcement off or error fallback: silent allow
        return None

    if result.decision == "allow" and result.context:
        # Permitted, but surface state context as a nudge
        lines = [f"Process: {result.reason}"]
        if result.guidance:
            lines.append(result.guidance)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": "\n".join(lines),
            }
        }

    if result.decision == "warn":
        # Monitor mode: allow but warn
        lines = [f"WARNING: {result.reason}"]
        if result.guidance:
            lines.append(result.guidance)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": "\n".join(lines),
            }
        }

    # Deny
    lines = [result.reason]
    if result.guidance:
        lines.append(result.guidance)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "\n".join(lines),
        }
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
            return

        # Extract file path from tool input for contextual suggestions
        tool_input = hook_input.get("tool_input", {})
        file_path = tool_input.get("file_path", "")

        project_root = Path(cwd)
        result = check_enforcement(
            project_root, action="edit", file_path=file_path
        )
        response = _to_hook_response(result, project_root)

        if response is not None:
            print(json.dumps(response))

    except Exception:
        # Fail open: if anything goes wrong, allow the action
        pass


# ---------------------------------------------------------------------------
# Hook installation helpers (platform-specific)
# ---------------------------------------------------------------------------


TURNSTILE_REF = "v0.1.3"


def pin_repo_url(repo_url: str, ref: str = TURNSTILE_REF) -> str:
    """Pin a git repo URL to a release tag for reproducible uvx installs.

    uvx caches by ref, so an unpinned URL freezes on whatever commit it first
    resolved; pinning to a tag makes upgrades explicit (bump the tag and
    `uvx --refresh`). A URL that already carries a ref is returned unchanged.
    """
    repo_url = repo_url.rstrip("/")
    if "@" in repo_url.rsplit("/", 1)[-1]:
        return repo_url
    return f"{repo_url}@{ref}"


def _guard_command_uvx(repo_url: str) -> str:
    """Build the guard command using uvx, pinned to the release tag."""
    return (
        f'uvx --python 3.12 --from "turnstile-cli @ '
        f'git+{pin_repo_url(repo_url)}#subdirectory=packages/turnstile-cli" '
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


def _strip_turnstile_hooks(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove turnstile guard hook entries from PreToolUse groups.

    Filters at the individual hook level so non-turnstile hooks sharing a
    group with the turnstile guard are preserved. A group is dropped only
    when the removal leaves it with no hooks; untouched groups pass through
    unchanged.
    """
    stripped = []
    for group in groups:
        group_hooks = group.get("hooks", [])
        remaining = [
            hk for hk in group_hooks
            if "turnstile guard" not in hk.get("command", "")
        ]
        if len(remaining) == len(group_hooks):
            stripped.append(group)
        elif remaining:
            stripped.append({**group, "hooks": remaining})
    return stripped


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
            settings = _load_settings(settings_path)
            hooks = settings.get("hooks", {})
            pre_tool = hooks.get("PreToolUse", [])
            # Remove turnstile entries, preserving co-grouped hooks
            pre_tool = _strip_turnstile_hooks(pre_tool)
            if pre_tool:
                hooks["PreToolUse"] = pre_tool
            else:
                hooks.pop("PreToolUse", None)
            if hooks:
                settings["hooks"] = hooks
            else:
                settings.pop("hooks", None)
            _atomic_write_text(
                settings_path, json.dumps(settings, indent=2) + "\n"
            )

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
        settings = _load_settings(settings_path)
    else:
        settings = {}

    # Merge hook config: replace any existing turnstile PreToolUse entries
    existing_hooks = settings.get("hooks", {})
    existing_pre_tool = existing_hooks.get("PreToolUse", [])

    # Remove old turnstile entries, preserving co-grouped hooks
    existing_pre_tool = _strip_turnstile_hooks(existing_pre_tool)

    # Add new turnstile entry
    existing_pre_tool.extend(hook_config["hooks"]["PreToolUse"])

    existing_hooks["PreToolUse"] = existing_pre_tool
    settings["hooks"] = existing_hooks

    _atomic_write_text(settings_path, json.dumps(settings, indent=2) + "\n")

    return {
        "installed": True,
        "mode": mode,
        "settings_path": str(settings_path),
        "message": f"Enforcement hook installed ({mode} mode)",
    }
