"""Commands about process definitions: validate, list, info, graph,
dry-run, schema export."""

from __future__ import annotations

import json
from pathlib import Path

import click

from turnstile_core.definition.analysis import generate_mermaid, simulate_dry_run
from turnstile_core.definition.inheritance import resolve_inheritance
from turnstile_core.definition.loader import (
    is_override_file,
    load_definition,
    load_override,
)
from turnstile_core.definition.model import ProcessDefinition
from turnstile_core.definition.schema import export_schemas

from turnstile_cli.context import get_engine, project_root


def _load_definition_or_override(path: Path) -> ProcessDefinition:
    """Load a YAML file as either a definition or an override with inheritance."""
    if is_override_file(path):
        override = load_override(path)
        parent_name = override.extends.rsplit("/", 1)[-1]
        parent_path = path.parent / f"{parent_name}.yaml"
        if not parent_path.exists():
            raise click.ClickException(
                f"Override extends '{override.extends}' but "
                f"'{parent_path}' not found. Parent must be in the same directory."
            )
        parent = load_definition(parent_path)
        return resolve_inheritance(override, parent)
    return load_definition(path)


@click.command()
@click.argument("path", type=click.Path(exists=True))
def validate(path: str) -> None:
    """Validate a process definition YAML file."""
    try:
        defn = _load_definition_or_override(Path(path))
        click.echo(f"Valid: {defn.name} v{defn.version}")
        click.echo(f"  States: {', '.join(s.id for s in defn.states)}")
        initial = defn.initial_state()
        click.echo(f"  Initial: {initial.id}")
        terminals = [s.id for s in defn.states if s.type.value == "terminal"]
        click.echo(f"  Terminal: {', '.join(terminals)}")
    except Exception as e:
        click.echo(f"Invalid: {e}", err=True)
        raise SystemExit(1)


@click.command("list")
@click.pass_context
def list_processes(ctx: click.Context) -> None:
    """List available process definitions."""
    engine = get_engine(ctx.obj["project"])
    procs = engine.list_processes()
    if not procs:
        click.echo("No process definitions found.")
        click.echo("Create .processes/*.yaml files to define processes.")
        return
    for p in procs:
        click.echo(f"  {p['name']} v{p['version']} - {p['description']}")


@click.command()
@click.argument("name")
@click.pass_context
def info(ctx: click.Context, name: str) -> None:
    """Show detailed info about a process definition.

    Displays parameters (required/optional, defaults), states with
    descriptions and permissions, and metadata. Use this to discover
    what parameters are needed before starting a process.
    """
    engine = get_engine(ctx.obj["project"])
    try:
        result = engine.info(name)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    click.echo(f"{result['name']} v{result['version']}")
    if result.get("description"):
        click.echo(f"  {result['description']}")

    click.echo("\nParameters:")
    for p in result["parameters"]:
        req = "required" if p["required"] else "optional"
        default = f", default: {p['default']}" if "default" in p else ""
        click.echo(f"  {p['name']} ({req}{default})")
        if p.get("description"):
            click.echo(f"    {p['description']}")

    click.echo("\nStates:")
    for s in result["states"]:
        parts = [f"  {s['id']}"]
        if s["type"] != "normal":
            parts.append(f"[{s['type']}]")
        if s.get("permissions", {}).get("edit") is False:
            parts.append("[read-only]")
        click.echo(" ".join(parts))
        if s.get("description"):
            click.echo(f"    {s['description']}")
        if s.get("transitions"):
            click.echo(f"    -> {', '.join(s['transitions'])}")


@click.command()
@click.argument("name", required=False, default=None)
@click.option(
    "--file",
    "-f",
    "file_path",
    type=click.Path(exists=True),
    default=None,
    help="Load definition from a YAML file instead of the registry.",
)
@click.pass_context
def graph(ctx: click.Context, name: str | None, file_path: str | None) -> None:
    """Generate a Mermaid state diagram for a process."""
    if file_path:
        defn = _load_definition_or_override(Path(file_path))
        result = generate_mermaid(defn)
    elif name:
        engine = get_engine(ctx.obj["project"])
        result = engine.graph(name)
    else:
        click.echo("Error: provide a process NAME or --file PATH", err=True)
        raise SystemExit(1)
    click.echo(result["mermaid_source"])


@click.command("dry-run")
@click.argument("name", required=False, default=None)
@click.option(
    "--file",
    "-f",
    "file_path",
    type=click.Path(exists=True),
    default=None,
    help="Load definition from a YAML file instead of the registry.",
)
@click.option(
    "--path",
    "-s",
    "state_path",
    multiple=True,
    help="Simulate a specific path (repeat for each state).",
)
@click.pass_context
def dry_run(
    ctx: click.Context,
    name: str | None,
    file_path: str | None,
    state_path: tuple[str, ...],
) -> None:
    """Simulate a process execution without running commands."""
    if file_path:
        defn = _load_definition_or_override(Path(file_path))
        path = list(state_path) if state_path else None
        steps = simulate_dry_run(defn, path)
    elif name:
        engine = get_engine(ctx.obj["project"])
        path = list(state_path) if state_path else None
        steps = engine.dry_run(name, path)
    else:
        click.echo("Error: provide a process NAME or --file PATH", err=True)
        raise SystemExit(1)

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


@click.command()
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
        out_dir = project_root(ctx) / ".processes" / "schemas"

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
