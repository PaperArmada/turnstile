"""Enforcement commands: the guard entry point and mode management."""

from __future__ import annotations

import json

import click

from turnstile_core.definition.loader import load_registry
from turnstile_core.ops.guard import (
    find_turnstile_root,
    install_enforcement,
    run_guard,
    update_registry_enforcement,
)

from turnstile_cli.context import TURNSTILE_REPO_URL, project_root


@click.command()
def guard() -> None:
    """Run the enforcement guard (called by Claude Code hooks).

    Reads a JSON hook payload from stdin, checks whether an active
    turnstile process exists, and outputs a hook response. This command
    is not meant to be run manually; it is invoked by the Claude Code
    PreToolUse hook.
    """
    run_guard()


@click.group()
def enforce() -> None:
    """Manage enforcement mode."""


@enforce.command("status")
@click.pass_context
def enforce_status(ctx: click.Context) -> None:
    """Show the current enforcement mode."""
    root = project_root(ctx)
    registry = load_registry(root)
    mode = registry.settings.enforcement
    click.echo(f"Enforcement mode: {mode}")

    # Check .claude/settings.json for hook
    settings_path = root / ".claude" / "settings.json"
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
        pre_tool = settings.get("hooks", {}).get("PreToolUse", [])
        has_hook = any(
            "turnstile guard" in hk.get("command", "")
            for entry in pre_tool
            for hk in entry.get("hooks", [])
        )
        if has_hook:
            click.echo("Claude Code hook: installed")
        else:
            click.echo("Claude Code hook: not installed")
    else:
        click.echo("Claude Code hook: not installed (.claude/settings.json missing)")


@enforce.command("on")
@click.option("--dev", is_flag=True, default=False, help="Use dev mode (uv --directory).")
@click.option("--turnstile-dir", default=None, help="Path to turnstile repo (dev mode).")
@click.option("--repo", default=None, help="Git repo URL (uvx mode).")
@click.pass_context
def enforce_on(ctx: click.Context, dev: bool, turnstile_dir: str | None, repo: str | None) -> None:
    """Enable enforcement (blocks file mutations without an active process)."""
    root = project_root(ctx)
    update_registry_enforcement(root, "enforce")
    if dev:
        result = install_enforcement(
            root, "enforce", turnstile_dir=turnstile_dir or find_turnstile_root()
        )
    else:
        result = install_enforcement(
            root, "enforce", repo_url=repo or TURNSTILE_REPO_URL
        )
    click.echo(f"Enforcement enabled: {result['message']}")
    click.echo(f"  Settings: {result.get('settings_path', 'N/A')}")


@enforce.command("monitor")
@click.option("--dev", is_flag=True, default=False, help="Use dev mode (uv --directory).")
@click.option("--turnstile-dir", default=None, help="Path to turnstile repo (dev mode).")
@click.option("--repo", default=None, help="Git repo URL (uvx mode).")
@click.pass_context
def enforce_monitor(ctx: click.Context, dev: bool, turnstile_dir: str | None, repo: str | None) -> None:
    """Enable monitor mode (warns but allows mutations without a process)."""
    root = project_root(ctx)
    update_registry_enforcement(root, "monitor")
    if dev:
        result = install_enforcement(
            root, "monitor", turnstile_dir=turnstile_dir or find_turnstile_root()
        )
    else:
        result = install_enforcement(
            root, "monitor", repo_url=repo or TURNSTILE_REPO_URL
        )
    click.echo(f"Monitor mode enabled: {result['message']}")
    click.echo(f"  Settings: {result.get('settings_path', 'N/A')}")


@enforce.command("off")
@click.pass_context
def enforce_off(ctx: click.Context) -> None:
    """Disable enforcement and remove the Claude Code hook."""
    root = project_root(ctx)
    update_registry_enforcement(root, "off")
    result = install_enforcement(root, "off")
    click.echo(f"Enforcement disabled: {result['message']}")
