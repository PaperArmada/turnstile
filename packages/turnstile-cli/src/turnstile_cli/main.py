"""Turnstile CLI: command-line interface for the process engine."""

from __future__ import annotations

import json
from pathlib import Path

import click

from turnstile_core.engine import Engine
from turnstile_core.loader import load_definition


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


if __name__ == "__main__":
    cli()
