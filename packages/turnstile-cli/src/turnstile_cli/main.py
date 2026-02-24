"""Turnstile CLI: command-line interface for the process engine."""

from __future__ import annotations

import json
from pathlib import Path

import click

from turnstile_core.engine import Engine
from turnstile_core.guard import (
    check_enforcement,
    install_enforcement,
    run_guard,
    update_registry_enforcement,
)
from turnstile_core.hooks import generate_hook, install_hook, uninstall_hook
from turnstile_core.loader import load_definition, load_registry
from turnstile_core.schema import export_schemas
from turnstile_core.template import scaffold_package


def _get_engine(project_root: str | None = None) -> Engine:
    root = Path(project_root) if project_root else Path.cwd()
    return Engine(root)


@click.group()
@click.option(
    "--project",
    "-p",
    default=None,
    help="Project root directory (defaults to CWD).",
)
@click.pass_context
def cli(ctx: click.Context, project: str | None) -> None:
    """Turnstile: process enforcement engine for development workflows."""
    ctx.ensure_object(dict)
    ctx.obj["project"] = project


@cli.command()
@click.argument("path", type=click.Path(exists=True))
def validate(path: str) -> None:
    """Validate a process definition YAML file."""
    try:
        defn = load_definition(Path(path))
        click.echo(f"Valid: {defn.name} v{defn.version}")
        click.echo(f"  States: {', '.join(s.id for s in defn.states)}")
        initial = defn.initial_state()
        click.echo(f"  Initial: {initial.id}")
        terminals = [s.id for s in defn.states if s.type.value == "terminal"]
        click.echo(f"  Terminal: {', '.join(terminals)}")
    except Exception as e:
        click.echo(f"Invalid: {e}", err=True)
        raise SystemExit(1)


@cli.command("list")
@click.pass_context
def list_processes(ctx: click.Context) -> None:
    """List available process definitions."""
    engine = _get_engine(ctx.obj["project"])
    procs = engine.list_processes()
    if not procs:
        click.echo("No process definitions found.")
        click.echo("Create .processes/*.yaml files to define processes.")
        return
    for p in procs:
        click.echo(f"  {p['name']} v{p['version']} - {p['description']}")


@cli.command()
@click.option("--all", "-a", "show_all", is_flag=True, help="Show all details.")
@click.pass_context
def status(ctx: click.Context, show_all: bool) -> None:
    """Show active process instances."""
    engine = _get_engine(ctx.obj["project"])
    instances = engine.status()
    if not instances:
        click.echo("No active process instances.")
        return
    for inst in instances:
        click.echo(
            f"  [{inst['instance_id']}] {inst['process_name']} "
            f"@ {inst['current_state']}"
        )
        if show_all:
            click.echo(f"    Started: {inst['started_at']}")
            click.echo(f"    Updated: {inst['updated_at']}")
            click.echo(
                f"    Next: {', '.join(inst['available_transitions'])}"
            )


@cli.command()
@click.argument("name")
@click.pass_context
def graph(ctx: click.Context, name: str) -> None:
    """Generate a Mermaid state diagram for a process."""
    engine = _get_engine(ctx.obj["project"])
    result = engine.graph(name)
    click.echo(result["mermaid_source"])


@cli.command("dry-run")
@click.argument("name")
@click.option(
    "--path",
    "-s",
    "state_path",
    multiple=True,
    help="Simulate a specific path (repeat for each state).",
)
@click.pass_context
def dry_run(ctx: click.Context, name: str, state_path: tuple[str, ...]) -> None:
    """Simulate a process execution without running commands."""
    engine = _get_engine(ctx.obj["project"])
    path = list(state_path) if state_path else None
    steps = engine.dry_run(name, path)

    for step in steps:
        if "step" in step:
            # Path simulation
            legal = "OK" if step["legal"] else "ILLEGAL"
            click.echo(
                f"  {step['step']}. {step['from_state']} -> "
                f"{step['to_state']} [{legal}]"
            )
        else:
            # Full description
            marker = ""
            if step["type"] == "initial":
                marker = " [initial]"
            elif step["type"] == "terminal":
                marker = " [terminal]"
            click.echo(f"  {step['state']}{marker}")
            if step["description"]:
                click.echo(f"    {step['description']}")

        # Show validations
        for phase in ("on_enter", "on_exit"):
            vals = step.get(f"{phase}_validations", [])
            if vals:
                click.echo(f"    {phase}:")
                for v in vals:
                    if v["type"].startswith("composite_"):
                        click.echo(
                            f"      {v['type']}: {v['message']} "
                            f"({len(v['rules'])} rules)"
                        )
                    else:
                        click.echo(
                            f"      [{v['severity']}] {v['message'] or v['command']}"
                        )

        # Show transitions for full description mode
        if "transitions" in step and step.get("transitions"):
            click.echo(f"    -> {', '.join(step['transitions'])}")


@cli.command()
@click.option(
    "--output",
    "-o",
    default=None,
    type=click.Path(),
    help="Output directory (defaults to .processes/schemas/).",
)
@click.pass_context
def schema(ctx: click.Context, output: str | None) -> None:
    """Export JSON Schema files for editor autocomplete."""
    if output:
        out_dir = Path(output)
    else:
        root = Path(ctx.obj["project"]) if ctx.obj["project"] else Path.cwd()
        out_dir = root / ".processes" / "schemas"

    written = export_schemas(out_dir)
    for name, path in written.items():
        click.echo(f"  {name}: {path}")
    click.echo(f"\nSchemas written to {out_dir}")
    click.echo(
        "\nTo enable YAML autocomplete in VS Code, add to .vscode/settings.json:"
    )
    click.echo(json.dumps({
        "yaml.schemas": {
            str(out_dir / "process-definition.schema.json"):
                ".processes/*.yaml",
            str(out_dir / "registry.schema.json"):
                ".processes/registry.yaml",
        }
    }, indent=2))


@cli.command("check-completed")
@click.argument("name")
@click.option(
    "--state",
    "-s",
    default=None,
    help="Required state that must have been reached.",
)
@click.option(
    "--parameter",
    "-P",
    "parameters",
    multiple=True,
    help="Parameter filter as key=value (repeat for multiple).",
)
@click.option(
    "--include-active",
    is_flag=True,
    help="Also search active (in-progress) instances.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def check_completed(
    ctx: click.Context,
    name: str,
    state: str | None,
    parameters: tuple[str, ...],
    include_active: bool,
    as_json: bool,
) -> None:
    """Check that a process instance was completed (for CI/hooks).

    Exits 0 if a matching instance is found, 1 otherwise.

    Examples:

      turnstile check-completed feature-deploy --state merge

      turnstile check-completed feature-deploy -P branch_name=main -s review_ready
    """
    engine = _get_engine(ctx.obj["project"])

    # Parse key=value parameters
    param_dict: dict[str, str] = {}
    for p in parameters:
        if "=" not in p:
            click.echo(f"Invalid parameter format: '{p}' (expected key=value)", err=True)
            raise SystemExit(1)
        key, value = p.split("=", 1)
        param_dict[key] = value

    result = engine.check_completed(
        process_name=name,
        state=state,
        parameters=param_dict or None,
        include_active=include_active,
    )

    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        if result["passed"]:
            click.echo(f"PASS: {result['message']}")
            for m in result["matches"]:
                click.echo(
                    f"  [{m['instance_id']}] {m['status']} "
                    f"@ {m['current_state']}"
                )
        else:
            click.echo(f"FAIL: {result['message']}", err=True)

    raise SystemExit(0 if result["passed"] else 1)


@cli.group()
def hooks() -> None:
    """Manage git hook helpers."""


@hooks.command("install")
@click.argument("hook_type", type=click.Choice(["pre-push", "pre-commit"]))
@click.option(
    "--process",
    "-p",
    "processes",
    multiple=True,
    required=True,
    help="Process check as 'name[:state]' (repeat for multiple).",
)
@click.option("--force", is_flag=True, help="Overwrite existing non-turnstile hooks.")
@click.pass_context
def hooks_install(
    ctx: click.Context,
    hook_type: str,
    processes: tuple[str, ...],
    force: bool,
) -> None:
    """Install a git hook that checks process completion.

    Examples:

      turnstile hooks install pre-push -p feature-deploy:merge

      turnstile hooks install pre-push -p feature-deploy:merge -p release:approved
    """
    checks = []
    for proc_spec in processes:
        if ":" in proc_spec:
            name, state = proc_spec.split(":", 1)
            checks.append({"process": name, "state": state})
        else:
            checks.append({"process": proc_spec})

    script = generate_hook(hook_type, checks)
    root = Path(ctx.obj["project"]) if ctx.obj.get("project") else Path.cwd()
    result = install_hook(hook_type, script, root, force=force)

    if result.get("installed"):
        click.echo(f"Installed {hook_type} hook at {result['path']}")
        if result.get("replaced"):
            click.echo("  (replaced existing turnstile hook)")
        if result.get("backup"):
            click.echo(f"  (backed up existing hook to {result['backup']})")
    else:
        click.echo(f"Failed: {result.get('error', 'unknown error')}", err=True)
        raise SystemExit(1)


@hooks.command("uninstall")
@click.argument("hook_type", type=click.Choice(["pre-push", "pre-commit"]))
@click.pass_context
def hooks_uninstall(ctx: click.Context, hook_type: str) -> None:
    """Remove a turnstile-generated git hook."""
    root = Path(ctx.obj["project"]) if ctx.obj.get("project") else Path.cwd()
    result = uninstall_hook(hook_type, root)

    if result.get("removed"):
        click.echo(f"Removed {hook_type} hook.")
        if result.get("restored_backup"):
            click.echo("  (restored previous hook from backup)")
    else:
        click.echo(f"Failed: {result.get('error', 'unknown error')}", err=True)
        raise SystemExit(1)


@hooks.command("show")
@click.argument("hook_type", type=click.Choice(["pre-push", "pre-commit"]))
@click.option(
    "--process",
    "-p",
    "processes",
    multiple=True,
    required=True,
    help="Process check as 'name[:state]' (repeat for multiple).",
)
def hooks_show(hook_type: str, processes: tuple[str, ...]) -> None:
    """Preview a hook script without installing it."""
    checks = []
    for proc_spec in processes:
        if ":" in proc_spec:
            name, state = proc_spec.split(":", 1)
            checks.append({"process": name, "state": state})
        else:
            checks.append({"process": proc_spec})

    script = generate_hook(hook_type, checks)
    click.echo(script)


cli.add_command(hooks)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def analytics(ctx: click.Context, as_json: bool) -> None:
    """Show process analytics from archived instances."""
    engine = _get_engine(ctx.obj["project"])
    result = engine.analytics()

    if as_json:
        click.echo(json.dumps(result, indent=2))
        return

    if not result["processes"]:
        click.echo("No archived instances found.")
        return

    click.echo(f"Total archived instances: {result['total_instances']}\n")

    for name, stats in result["processes"].items():
        click.echo(f"  {name}:")
        click.echo(
            f"    Completed: {stats['completed']}, "
            f"Abandoned: {stats['abandoned']} "
            f"({stats['completion_rate']:.0%} completion rate)"
        )
        if stats["avg_duration_seconds"] is not None:
            dur = stats["avg_duration_seconds"]
            if dur < 60:
                click.echo(f"    Avg duration: {dur:.1f}s")
            elif dur < 3600:
                click.echo(f"    Avg duration: {dur / 60:.1f}m")
            else:
                click.echo(f"    Avg duration: {dur / 3600:.1f}h")

        if stats["state_avg_duration_seconds"]:
            click.echo("    Per-state avg:")
            for state, sdur in stats["state_avg_duration_seconds"].items():
                if sdur < 60:
                    click.echo(f"      {state}: {sdur:.1f}s")
                elif sdur < 3600:
                    click.echo(f"      {state}: {sdur / 60:.1f}m")
                else:
                    click.echo(f"      {state}: {sdur / 3600:.1f}h")

        if stats["total_overrides"]:
            click.echo(f"    Overrides: {stats['total_overrides']}")
            for pattern, count in stats["override_patterns"].items():
                click.echo(f"      {pattern}: {count}x")


@cli.command("init-package")
@click.argument("name")
@click.option(
    "--process",
    "-P",
    "processes",
    multiple=True,
    help="Process name to include (repeat for multiple).",
)
@click.option(
    "--output",
    "-o",
    default=None,
    type=click.Path(),
    help="Output directory (defaults to ./<name>/).",
)
def init_package(name: str, processes: tuple[str, ...], output: str | None) -> None:
    """Scaffold a new shared process package."""
    out_dir = Path(output) if output else Path.cwd() / name
    proc_list = list(processes) if processes else None

    created = scaffold_package(name, out_dir, proc_list)
    click.echo(f"Created package '{name}' at {out_dir}")
    for label in created:
        click.echo(f"  {label}")
    click.echo(f"\nNext steps:")
    click.echo(f"  1. Edit the process definitions in {out_dir}/src/")
    click.echo(f"  2. Build and publish: cd {out_dir} && uv build")


@cli.command()
def guard() -> None:
    """Run the enforcement guard (called by Claude Code hooks).

    Reads a JSON hook payload from stdin, checks whether an active
    turnstile process exists, and outputs a hook response. This command
    is not meant to be run manually; it is invoked by the Claude Code
    PreToolUse hook.
    """
    run_guard()


@cli.group()
def enforce() -> None:
    """Manage enforcement mode."""


@enforce.command("status")
@click.pass_context
def enforce_status(ctx: click.Context) -> None:
    """Show the current enforcement mode."""
    root = Path(ctx.obj["project"]) if ctx.obj.get("project") else Path.cwd()
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
@click.option(
    "--turnstile-dir",
    default=None,
    help="Path to turnstile repo (auto-detected if omitted).",
)
@click.pass_context
def enforce_on(ctx: click.Context, turnstile_dir: str | None) -> None:
    """Enable enforcement (blocks file mutations without an active process)."""
    root = Path(ctx.obj["project"]) if ctx.obj.get("project") else Path.cwd()
    update_registry_enforcement(root, "enforce")
    result = install_enforcement(root, "enforce", turnstile_dir)
    click.echo(f"Enforcement enabled: {result['message']}")
    click.echo(f"  Settings: {result.get('settings_path', 'N/A')}")


@enforce.command("monitor")
@click.option(
    "--turnstile-dir",
    default=None,
    help="Path to turnstile repo (auto-detected if omitted).",
)
@click.pass_context
def enforce_monitor(ctx: click.Context, turnstile_dir: str | None) -> None:
    """Enable monitor mode (warns but allows mutations without a process)."""
    root = Path(ctx.obj["project"]) if ctx.obj.get("project") else Path.cwd()
    update_registry_enforcement(root, "monitor")
    result = install_enforcement(root, "monitor", turnstile_dir)
    click.echo(f"Monitor mode enabled: {result['message']}")
    click.echo(f"  Settings: {result.get('settings_path', 'N/A')}")


@enforce.command("off")
@click.pass_context
def enforce_off(ctx: click.Context) -> None:
    """Disable enforcement and remove the Claude Code hook."""
    root = Path(ctx.obj["project"]) if ctx.obj.get("project") else Path.cwd()
    update_registry_enforcement(root, "off")
    result = install_enforcement(root, "off")
    click.echo(f"Enforcement disabled: {result['message']}")


if __name__ == "__main__":
    cli()
