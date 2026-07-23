"""Turnstile CLI: command-line interface for the process engine."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

import click

from turnstile_core.admin import generate_mermaid, simulate_dry_run
from turnstile_core.engine import Engine
from turnstile_core.graph import analyze as graph_analyze
from turnstile_core.guard import (
    _find_turnstile_root,
    install_enforcement,
    pin_repo_url,
    run_guard,
    update_registry_enforcement,
)
from turnstile_core.errors import InstanceNotFoundError, ProcessNotFoundError
from turnstile_core.hooks import generate_hook, install_hook, uninstall_hook
from turnstile_core.inheritance import resolve_inheritance
from turnstile_core.loader import (
    _is_override_file,
    load_definition,
    load_override,
    load_registry,
)
from turnstile_core.schema import export_schemas
from turnstile_core.template import scaffold_package

from turnstile_cli.principles import PRINCIPLES
from turnstile_cli.starters import STARTER_REGISTRY, STARTERS


TURNSTILE_REPO_URL = "https://github.com/PaperArmada/turnstile.git"


def _mcp_config_uvx(repo_url: str) -> dict:
    """Generate .mcp.json config using uvx (no local clone needed)."""
    return {
        "mcpServers": {
            "turnstile": {
                "type": "stdio",
                "command": "uvx",
                "args": [
                    "--python", "3.12",
                    "--from",
                    f"turnstile-mcp @ git+{pin_repo_url(repo_url)}"
                    f"#subdirectory=packages/turnstile-mcp",
                    "python", "-m", "turnstile_mcp.server",
                ],
            }
        }
    }


def _mcp_config_dev(turnstile_dir: str) -> dict:
    """Generate .mcp.json config using uv --directory (for development)."""
    return {
        "mcpServers": {
            "turnstile": {
                "type": "stdio",
                "command": "bash",
                "args": [
                    "-c",
                    (
                        f"TURNSTILE_PROJECT_DIR=$(pwd) "
                        f"uv --directory {turnstile_dir} "
                        f"run --package turnstile-mcp "
                        f"python -m turnstile_mcp.server"
                    ),
                ],
            }
        }
    }


def _get_engine(project_root: str | None = None) -> Engine:
    root = Path(project_root) if project_root else Path.cwd()
    return Engine(root)


def _exit_process_not_found(
    project_root: str | None, name: str, exc: Exception
) -> NoReturn:
    """Report an unresolvable process name and exit 1.

    When the error concerns the queried name and a matching file exists in
    .processes/ unregistered, say so: a non-empty registry.local disables
    auto-discovery, so "valid file, unknown process" is a registration
    problem, not a YAML problem. The name check matters because the engine
    can also raise about a *different* process (e.g. a registry entry whose
    file is missing), where the hint would mislead.
    """
    click.echo(f"Error: {exc}", err=True)
    root = Path(project_root) if project_root else Path.cwd()
    candidate = root / ".processes" / f"{name}.yaml"
    if f"'{name}'" in str(exc) and candidate.exists():
        try:
            registered: list[str] | None = load_registry(root).local or []
        except Exception:
            registered = None
        if registered is not None and name not in registered:
            click.echo(
                f".processes/{name}.yaml exists but is not registered. "
                f'Add "- {name}" under "local:" in .processes/registry.yaml.',
                err=True,
            )
        elif registered is not None:
            try:
                actual = load_definition(candidate).name
            except Exception:
                actual = None
            if actual and actual != name:
                click.echo(
                    f'.processes/{name}.yaml is registered but defines '
                    f'"name: {actual}"; processes are addressed by that '
                    f'internal "name:" field. Try \'{actual}\'.',
                    err=True,
                )
            else:
                click.echo(
                    f".processes/{name}.yaml is registered, but processes "
                    'are addressed by their internal "name:" field, which '
                    "may differ from the filename.",
                    err=True,
                )
    raise SystemExit(1)


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
    # When running via `uv --directory`, CWD is the turnstile source repo,
    # not the consumer project. TURNSTILE_PROJECT_DIR overrides CWD.
    if project is None:
        project = os.environ.get("TURNSTILE_PROJECT_DIR")
    ctx.obj["project"] = project


def _preexisting_definition_stems(proc_dir: Path) -> list[str]:
    """Stems of process files that predate init's starter pack.

    Covers both plain definitions and top-level override files: a non-empty
    registry.local disables auto-discovery entirely, so anything left off the
    list becomes silently unstartable. Excludes registry.yaml and starter
    filenames. Files that do not parse, and overrides whose extends target
    will not exist post-init, are skipped with a warning rather than
    registered: registry-listed files are on the loader's strict path, where
    any load or resolution failure fails hard in every discovery and would
    break all name-based commands.
    """
    stems: list[str] = []
    known_names = {Path(filename).stem for filename in STARTERS}
    overrides: list[tuple[Path, str]] = []
    for path in sorted(proc_dir.glob("*.yaml")):
        if path.name == "registry.yaml" or path.name in STARTERS:
            continue
        try:
            if _is_override_file(path):
                overrides.append((path, load_override(path).extends))
                continue
            defn = load_definition(path)
        except Exception as e:
            click.echo(
                f"Warning: {path.name} is not a loadable process definition "
                f"({e}); not registered. Fix it and add it to "
                ".processes/registry.yaml manually.",
                err=True,
            )
            continue
        known_names.add(defn.name)
        stems.append(path.stem)
    for path, parent in overrides:
        if parent in known_names:
            stems.append(path.stem)
        else:
            click.echo(
                f"Warning: {path.name} extends '{parent}', which will not "
                "exist after init; not registered. Fix the extends target "
                "and add it to .processes/registry.yaml manually.",
                err=True,
            )
    return stems


def _load_definition_or_override(path: Path) -> "ProcessDefinition":
    """Load a YAML file as either a definition or an override with inheritance."""
    from turnstile_core.models import ProcessDefinition

    if _is_override_file(path):
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


@cli.command()
@click.argument("path", type=click.Path(exists=True))
def validate(path: str) -> None:
    """Validate a process definition YAML file."""
    try:
        defn = _load_definition_or_override(Path(path))
        analysis = graph_analyze(defn)
    except Exception as e:
        click.echo(f"Invalid: {e}", err=True)
        raise SystemExit(1)
    if analysis.errors:
        click.echo(f"Invalid: {defn.name} v{defn.version}", err=True)
        for error in analysis.errors:
            click.echo(f"  Error: {error}", err=True)
        raise SystemExit(1)
    click.echo(f"Valid: {defn.name} v{defn.version}")
    click.echo(f"  States: {', '.join(s.id for s in defn.states)}")
    initial = defn.initial_state()
    click.echo(f"  Initial: {initial.id}")
    terminals = [s.id for s in defn.states if s.type.value == "terminal"]
    click.echo(f"  Terminal: {', '.join(terminals)}")
    for warning in analysis.warnings:
        click.echo(f"  Warning: {warning}")


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
@click.argument("name")
@click.pass_context
def info(ctx: click.Context, name: str) -> None:
    """Show detailed info about a process definition.

    Displays parameters (required/optional, defaults), states with
    descriptions and permissions, and metadata. Use this to discover
    what parameters are needed before starting a process.
    """
    try:
        engine = _get_engine(ctx.obj["project"])
        result = engine.info(name)
    except ProcessNotFoundError as e:
        _exit_process_not_found(ctx.obj["project"], name, e)
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


def _render_value(value: Any, limit: int = 200) -> str:
    """Make a recorded value safe to echo in the trajectory render.

    Every rendered field is writable by the agent under review (transition
    metadata, gate output, skip reasons), so control characters must not
    reach the terminal: a \\r or ANSI escape could redraw the line and spoof
    the audit view a human is reading. Escapes C0 controls and DEL visibly,
    JSON-encodes non-scalar values, and truncates long ones.
    """
    if isinstance(value, (dict, list)):
        text = json.dumps(value)
    else:
        text = str(value)
    text = re.sub(
        r"[\x00-\x1f\x7f]",
        lambda m: repr(m.group())[1:-1],
        text,
    )
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


@cli.command()
@click.argument("instance_id")
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the full record as JSON."
)
@click.pass_context
def history(ctx: click.Context, instance_id: str, as_json: bool) -> None:
    """Show the recorded trajectory of a process instance.

    Renders every transition with any recorded actor attribution
    (triggered_by, role, session), validation gate results, and
    structured metadata, followed by the override log. Searches active,
    completed, and abandoned instances, so completed work stays
    reviewable.
    """
    try:
        engine = _get_engine(ctx.obj["project"])
        record = engine.trajectory(instance_id)
    except Exception as e:
        # InstanceNotFoundError, torn/hand-edited state files, broken
        # registry: all surface as one readable line, never a traceback.
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps(record, indent=2))
        return

    status = record["status"]
    if record["waiting"]:
        status = f"{status} (waiting for signal)"
    elif record["suspended"]:
        status = f"{status} (suspended on subprocess)"
    click.echo(
        f"{_render_value(record['process_name'])} "
        f"[{record['instance_id']}] "
        f"{status} @ {_render_value(record['current_state'])}"
    )
    if record["parameters"]:
        params = ", ".join(
            f"{_render_value(k)}={_render_value(v)}"
            for k, v in record["parameters"].items()
        )
        click.echo(f"  parameters: {params}")
    started_by = (
        f" by {_render_value(record['started_by'])}"
        if record["started_by"]
        else ""
    )
    click.echo(f"  started {record['started_at']}{started_by}")
    click.echo(f"  updated {record['updated_at']}")
    if record["parent_instance_id"]:
        parent_state = (
            f" @ {_render_value(record['parent_state_id'])}"
            if record["parent_state_id"]
            else ""
        )
        click.echo(
            f"  parent: [{record['parent_instance_id']}]{parent_state}"
        )
    if record["child_instance_id"]:
        click.echo(f"  child: [{record['child_instance_id']}]")

    transitions = record["transitions"]
    if not transitions:
        click.echo("\nNo transitions recorded.")
    else:
        click.echo(f"\nTransitions ({len(transitions)}):")
        for t in transitions:
            actor_bits = [
                bit
                for bit in (
                    _render_value(t["triggered_by"])
                    if t["triggered_by"]
                    else "",
                    f"role={_render_value(t['role'])}" if t["role"] else "",
                    f"session={_render_value(t['session_id'])}"
                    if t["session_id"]
                    else "",
                )
                if bit
            ]
            actor = f"  ({', '.join(actor_bits)})" if actor_bits else ""
            click.echo(
                f"  {t['timestamp']}  "
                f"{_render_value(t['from_state'])} -> "
                f"{_render_value(t['to_state'])}{actor}"
            )
            for v in t["validations"]:
                if v.get("passed"):
                    mark = "ok"
                else:
                    mark = v.get("severity") or "error"
                label = v.get("message") or v.get("command", "")
                click.echo(f"      [{mark}] {_render_value(label)}")
                if not v.get("passed") and v.get("output"):
                    first_line = str(v["output"]).splitlines()[0]
                    click.echo(
                        f"          {_render_value(first_line, limit=120)}"
                    )
            for key, value in (t["metadata"] or {}).items():
                click.echo(
                    f"      {_render_value(key)}: {_render_value(value)}"
                )

    if record["overrides"]:
        click.echo(f"\nOverrides ({len(record['overrides'])}):")
        for o in record["overrides"]:
            actor = (
                _render_value(o["triggered_by"])
                if o.get("triggered_by")
                else "unknown"
            )
            click.echo(
                f"  {o['at']}  {_render_value(o['from_state'])} -> "
                f"{_render_value(o['to_state'])} "
                f"by {actor}: {_render_value(o['reason'])}"
            )


def _format_age(iso_timestamp: str) -> str:
    """Format an ISO timestamp as a human-readable age string."""
    try:
        ts = datetime.fromisoformat(iso_timestamp)
        now = datetime.now(timezone.utc)
        delta = now - ts
        hours = delta.total_seconds() / 3600
        if hours < 1:
            minutes = int(delta.total_seconds() / 60)
            return f"{minutes}m ago"
        elif hours < 24:
            return f"{int(hours)}h ago"
        else:
            days = int(hours / 24)
            return f"{days}d ago"
    except (ValueError, TypeError):
        return "unknown"


@cli.command()
@click.pass_context
def active(ctx: click.Context) -> None:
    """Show active processes with age (for session-start hooks).

    Designed for wiring into Claude Code SessionStart hooks. Shows
    a compact summary of active instances with staleness indicators.

    \b
    Example hook in .claude/settings.json:
      "hooks": {
        "SessionStart": [{
          "type": "command",
          "command": "turnstile active"
        }]
      }
    """
    engine = _get_engine(ctx.obj["project"])
    instances = engine.status()
    if not instances:
        return  # Silent when no active processes (clean session start)

    click.echo(f"Active processes ({len(instances)}):")
    for inst in instances:
        age = _format_age(inst["updated_at"])
        line = (
            f"  [{inst['instance_id']}] {inst['process_name']} "
            f"@ {inst['current_state']} (updated {age})"
        )
        # Staleness warning
        try:
            updated = datetime.fromisoformat(inst["updated_at"])
            hours = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
            if hours > 24:
                line += " ⚠ stale"
        except (ValueError, TypeError):
            pass

        if inst.get("suspended"):
            line += f" [suspended, child: {inst.get('child_instance_id', '?')}]"

        click.echo(line)

        # Show available transitions
        transitions = inst.get("available_transitions", [])
        if transitions:
            click.echo(f"    next: {', '.join(transitions)}")


@cli.command()
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
        try:
            engine = _get_engine(ctx.obj["project"])
            result = engine.graph(name)
        except ProcessNotFoundError as e:
            _exit_process_not_found(ctx.obj["project"], name, e)
    else:
        click.echo("Error: provide a process NAME or --file PATH", err=True)
        raise SystemExit(1)
    click.echo(result["mermaid_source"])


@cli.command("dry-run")
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
        path = list(state_path) if state_path else None
        try:
            engine = _get_engine(ctx.obj["project"])
            steps = engine.dry_run(name, path)
        except ProcessNotFoundError as e:
            _exit_process_not_found(ctx.obj["project"], name, e)
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
@click.option("--parent", default=None, help="Filter by parent instance ID (for dispatched children).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def check_completed(
    ctx: click.Context,
    name: str,
    state: str | None,
    parameters: tuple[str, ...],
    include_active: bool,
    parent: str | None,
    as_json: bool,
) -> None:
    """Check that a process instance was completed (for CI/hooks).

    Exits 0 if a matching instance is found, 1 otherwise.

    Examples:

      turnstile check-completed feature-deploy --state merge

      turnstile check-completed feature-deploy -P branch_name=main -s review_ready

      turnstile check-completed feature-development --parent abc123
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
        parent_instance_id=parent,
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


def _clone_source(repo_url: str, ref: str = "") -> Path:
    """Shallow-clone a turnstile source repo into a temp directory.

    Returns the path to the temp directory. Caller must clean up.
    """
    tmp = tempfile.mkdtemp(prefix="turnstile-update-")
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd.extend(["--branch", ref])
    cmd.extend([repo_url, tmp])
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return Path(tmp)


def _load_version(path: Path) -> str | None:
    """Load just the version from a YAML definition file."""
    try:
        defn = load_definition(path)
        return defn.version
    except Exception:
        return None


def _compare_definitions(
    local_dir: Path, source_dir: Path
) -> list[dict[str, str]]:
    """Compare local and source process definitions by version.

    Returns a list of dicts with keys: name, local_version, source_version,
    status (up-to-date, update-available, local-only, new).
    """
    results = []

    # Collect source definitions
    source_defs: dict[str, Path] = {}
    if source_dir.exists():
        for path in sorted(source_dir.glob("*.yaml")):
            if path.name == "registry.yaml":
                continue
            try:
                defn = load_definition(path)
                source_defs[defn.name] = path
            except Exception:
                continue

    # Collect local definitions
    local_defs: dict[str, Path] = {}
    if local_dir.exists():
        for path in sorted(local_dir.glob("*.yaml")):
            if path.name == "registry.yaml":
                continue
            try:
                defn = load_definition(path)
                local_defs[defn.name] = path
            except Exception:
                continue

    # Compare
    all_names = sorted(set(list(source_defs.keys()) + list(local_defs.keys())))
    for name in all_names:
        local_path = local_defs.get(name)
        source_path = source_defs.get(name)

        if local_path and source_path:
            local_ver = _load_version(local_path) or "?"
            source_ver = _load_version(source_path) or "?"
            if local_ver == source_ver:
                # Check if content differs even with same version
                local_content = local_path.read_text()
                source_content = source_path.read_text()
                if local_content == source_content:
                    status = "up-to-date"
                else:
                    status = "modified"
            else:
                status = "update-available"
            results.append({
                "name": name,
                "local_version": local_ver,
                "source_version": source_ver,
                "local_path": str(local_path),
                "source_path": str(source_path),
                "status": status,
            })
        elif local_path:
            local_ver = _load_version(local_path) or "?"
            results.append({
                "name": name,
                "local_version": local_ver,
                "source_version": "-",
                "local_path": str(local_path),
                "source_path": "",
                "status": "local-only",
            })
        elif source_path:
            source_ver = _load_version(source_path) or "?"
            results.append({
                "name": name,
                "local_version": "-",
                "source_version": source_ver,
                "local_path": "",
                "source_path": str(source_path),
                "status": "new",
            })

    return results


@cli.command()
@click.option(
    "--repo", default=None,
    help="Git repo URL (defaults to PaperArmada/turnstile).",
)
@click.option(
    "--ref", default="",
    help="Git ref to fetch (branch, tag). Defaults to the repo's default branch.",
)
@click.option(
    "--apply", "do_apply", is_flag=True, default=False,
    help="Apply updates (overwrite local files with source versions).",
)
@click.option(
    "--principles", "update_principles", is_flag=True, default=False,
    help="Also update design principles in .processes/principles/.",
)
@click.option(
    "--dev", is_flag=True, default=False,
    help="Use local turnstile repo instead of cloning from git.",
)
@click.option(
    "--turnstile-dir", default=None,
    help="Path to turnstile repo (dev mode, auto-detected if omitted).",
)
@click.pass_context
def update(
    ctx: click.Context,
    repo: str | None,
    ref: str,
    do_apply: bool,
    update_principles: bool,
    dev: bool,
    turnstile_dir: str | None,
) -> None:
    """Check for and apply updates to process definitions.

    Compares local .processes/*.yaml files against the source repo and
    shows which definitions have newer versions available. By default,
    only shows a comparison table. Use --apply to overwrite local files.

    \b
    Examples:
      turnstile update                  # Show what's available
      turnstile update --apply          # Apply definition updates
      turnstile update --principles     # Also refresh design principles
      turnstile update --dev            # Use local repo instead of git
    """
    root = Path(ctx.obj["project"]) if ctx.obj.get("project") else Path.cwd()
    local_dir = root / ".processes"

    if not local_dir.exists():
        click.echo(
            "No .processes/ directory found. Run 'turnstile init' first.",
            err=True,
        )
        raise SystemExit(1)

    # Resolve source directory
    source_tmp: Path | None = None
    try:
        if dev:
            source_root = Path(
                turnstile_dir or _find_turnstile_root()
            )
            source_dir = source_root / ".processes"
            if not source_dir.exists():
                click.echo(
                    f"No .processes/ in turnstile repo at {source_root}",
                    err=True,
                )
                raise SystemExit(1)
        else:
            repo_url = repo or TURNSTILE_REPO_URL
            click.echo(f"Fetching from {repo_url}...")
            try:
                source_tmp = _clone_source(repo_url, ref)
            except subprocess.CalledProcessError as e:
                click.echo(
                    f"Failed to clone: {e.stderr or e.stdout or str(e)}",
                    err=True,
                )
                raise SystemExit(1)
            except FileNotFoundError:
                click.echo("git is not installed or not in PATH", err=True)
                raise SystemExit(1)
            source_dir = source_tmp / ".processes"

        # Compare definitions
        comparisons = _compare_definitions(local_dir, source_dir)

        if not comparisons:
            click.echo("No process definitions found to compare.")
            return

        # Display comparison table
        has_updates = False
        click.echo(f"\n{'Name':<25} {'Local':<12} {'Source':<12} Status")
        click.echo("-" * 65)
        for c in comparisons:
            status_display = c["status"]
            if c["status"] == "update-available":
                status_display = "UPDATE AVAILABLE"
                has_updates = True
            elif c["status"] == "modified":
                status_display = "content differs (same version)"
                has_updates = True
            elif c["status"] == "new":
                status_display = "new in source"
                has_updates = True
            elif c["status"] == "local-only":
                status_display = "local only"
            click.echo(
                f"  {c['name']:<23} {c['local_version']:<12} "
                f"{c['source_version']:<12} {status_display}"
            )

        # Check principles
        principles_dir = local_dir / "principles"
        principles_stale = False
        if principles_dir.exists():
            for name, content in PRINCIPLES.items():
                local_file = principles_dir / f"{name}.md"
                if not local_file.exists() or local_file.read_text() != content:
                    principles_stale = True
                    break
        elif PRINCIPLES:
            principles_stale = True

        if principles_stale:
            click.echo(f"\nDesign principles: updates available")
        else:
            click.echo(f"\nDesign principles: up to date")

        if not has_updates and not principles_stale:
            click.echo("\nEverything is up to date.")
            return

        if not do_apply and not update_principles:
            click.echo(
                "\nRun with --apply to update definitions"
                " or --principles to update principles."
            )
            return

        # Apply updates
        updated = 0
        if do_apply:
            for c in comparisons:
                if c["status"] in ("update-available", "modified", "new"):
                    source_path = Path(c["source_path"])
                    if c["status"] == "new":
                        target = local_dir / source_path.name
                    else:
                        target = Path(c["local_path"])
                    shutil.copy2(source_path, target)
                    click.echo(f"  Updated: {target.name}")
                    updated += 1

        if update_principles:
            if not principles_dir.exists():
                principles_dir.mkdir(parents=True)
            written = 0
            for name, content in PRINCIPLES.items():
                target = principles_dir / f"{name}.md"
                if not target.exists() or target.read_text() != content:
                    target.write_text(content)
                    written += 1
            if written:
                click.echo(f"  Updated: {written} principle(s)")
                updated += written

        if updated:
            click.echo(f"\n{updated} file(s) updated.")
        else:
            click.echo("\nNo files were updated.")

    finally:
        if source_tmp and source_tmp.exists():
            shutil.rmtree(source_tmp, ignore_errors=True)


@cli.command()
@click.option(
    "--dev", is_flag=True, default=False,
    help="Use dev mode (uv --directory) instead of uvx.",
)
@click.option(
    "--turnstile-dir", default=None,
    help="Path to turnstile repo (dev mode only, auto-detected if omitted).",
)
@click.option(
    "--repo", default=None,
    help="Git repo URL (uvx mode, defaults to PaperArmada/turnstile).",
)
@click.option(
    "--enforce", "enforce_mode", default="monitor",
    type=click.Choice(["monitor", "enforce", "off"]),
    help="Enforcement mode (default: monitor).",
)
@click.option(
    "--connect-only", is_flag=True, default=False,
    help="Only set up MCP and hooks; skip .processes/, principles, and registry.",
)
@click.pass_context
def init(
    ctx: click.Context,
    dev: bool,
    turnstile_dir: str | None,
    repo: str | None,
    enforce_mode: str,
    connect_only: bool,
) -> None:
    """Initialize turnstile in a project directory.

    Sets up .mcp.json, .processes/, registry.yaml, and enforcement hooks.
    Safe to run in an existing project; will not overwrite existing files.

    Use --connect-only when cloning a repo that already has process
    definitions committed. This creates only the local wiring (.mcp.json,
    .claude/settings.json) without touching .processes/ or registry.yaml.

    By default, uses uvx to run turnstile directly from GitHub (no local
    clone needed). Use --dev for local development with live code changes.

    \b
    Examples:
      turnstile init
      turnstile init --connect-only
      turnstile init --dev
      turnstile init --enforce off
      turnstile init --repo https://github.com/myorg/turnstile.git
    """
    root = Path(ctx.obj["project"]) if ctx.obj.get("project") else Path.cwd()
    repo_url = repo or TURNSTILE_REPO_URL

    if dev and turnstile_dir is None:
        turnstile_dir = _find_turnstile_root()

    created: list[str] = []
    proc_dir = root / ".processes"

    if not connect_only:
        # .processes/ directory
        if not proc_dir.exists():
            proc_dir.mkdir(parents=True)
            created.append(".processes/")

        # Curated starter pack (bundled with the package, written offline).
        # Existing files are never overwritten; `turnstile update` is the
        # refresh path.
        starters_written = 0
        for filename, content in STARTERS.items():
            target = proc_dir / filename
            if not target.exists():
                target.write_text(content)
                starters_written += 1
        if starters_written:
            created.append(
                f".processes/ ({starters_written} starter definitions)"
            )

        # Design principles
        principles_dir = proc_dir / "principles"
        if not principles_dir.exists():
            principles_dir.mkdir(parents=True)
            written = 0
            for name, content in PRINCIPLES.items():
                (principles_dir / f"{name}.md").write_text(content)
                written += 1
            created.append(f".processes/principles/ ({written} files)")

        # registry.yaml — registers the starter pack and sets enforcement
        # mode. Definitions that were in .processes/ before init ran must be
        # folded into local:, because a non-empty local list disables
        # auto-discovery and would otherwise make them unstartable.
        registry_path = proc_dir / "registry.yaml"
        if not registry_path.exists():
            # Quote the mode: YAML 1.1 reads a bare `off` as boolean False,
            # which RegistryConfig rejects, bricking every command.
            content = STARTER_REGISTRY.replace(
                "__ENFORCEMENT__", f"'{enforce_mode}'"
            )
            preexisting = _preexisting_definition_stems(proc_dir)
            if preexisting:
                if "local:\n" not in content:
                    raise RuntimeError(
                        "STARTER_REGISTRY template drifted: 'local:' "
                        "anchor missing; cannot register existing files"
                    )
                # json.dumps yields a valid YAML scalar, so hostile stems
                # (colons, '#', leading '-') cannot corrupt the registry.
                content = content.replace(
                    "local:\n",
                    "local:\n"
                    + "".join(
                        f"- {json.dumps(stem)}\n" for stem in preexisting
                    ),
                    1,
                )
                created.append(
                    ".processes/registry.yaml (+ registered existing: "
                    f"{', '.join(preexisting)})"
                )
            else:
                created.append(".processes/registry.yaml")
            registry_path.write_text(content)
    else:
        # Validate that definitions exist when connecting
        if not proc_dir.exists():
            click.echo(
                "Warning: .processes/ not found. "
                "Use 'turnstile init' (without --connect-only) for new projects.",
                err=True,
            )
        registry_path = proc_dir / "registry.yaml"
        if registry_path.exists():
            click.echo(f"  Found {registry_path.relative_to(root)}")
        else:
            click.echo(
                "Warning: .processes/registry.yaml not found. "
                "Enforcement may not work without it.",
                err=True,
            )

    # .mcp.json
    mcp_path = root / ".mcp.json"
    if not mcp_path.exists():
        if dev:
            mcp_config = _mcp_config_dev(turnstile_dir)
        else:
            mcp_config = _mcp_config_uvx(repo_url)
        mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
        created.append(".mcp.json")
    else:
        click.echo("  .mcp.json already exists (skipped)")

    # .claude/settings.json (enforcement hook)
    if enforce_mode != "off":
        if dev:
            result = install_enforcement(
                root, enforce_mode, turnstile_dir=turnstile_dir
            )
        else:
            result = install_enforcement(
                root, enforce_mode, repo_url=repo_url
            )
        created.append(f".claude/settings.json ({enforce_mode} mode)")

    # .gitignore additions
    gitignore_path = root / ".gitignore"
    gitignore_entries = [".mcp.json", ".claude/settings.json", ".process-state/"]
    if gitignore_path.exists():
        existing = gitignore_path.read_text()
    else:
        existing = ""
    to_add = [e for e in gitignore_entries if e not in existing]
    if to_add:
        with open(gitignore_path, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(to_add) + "\n")
        created.append(f".gitignore (+{', '.join(to_add)})")

    if created:
        mode_label = "dev" if dev else "uvx"
        click.echo(f"Initialized turnstile in {root} ({mode_label} mode)")
        for f in created:
            click.echo(f"  {f}")
        if not dev:
            click.echo(
                "\nRestart Claude Code to connect the MCP server."
            )

    # Emit CLAUDE.md guidance for the agent to integrate
    claude_md = root / "CLAUDE.md"
    marker = "## Process Enforcement"
    already_has = claude_md.exists() and marker in claude_md.read_text()
    if not already_has:
        # Discover processes to build the guidance table
        proc_dir = root / ".processes"
        proc_table = ""
        if proc_dir.exists():
            from turnstile_core.loader import discover_definitions_full
            discovered = discover_definitions_full(root)
            if discovered:
                rows = []
                for name, disc in sorted(discovered.items()):
                    defn = disc.definition
                    params = ", ".join(
                        f'"{p.name}": "..."'
                        for p in defn.parameters
                        if p.required
                    )
                    rows.append(
                        f'| {defn.description or name} '
                        f'| `process_start("{name}", {{{params}}})` |'
                    )
                proc_table = (
                    "| Task | Command |\n"
                    "|------|---------|"
                )
                for row in rows:
                    proc_table += f"\n{row}"

        guidance = f"""{marker}

This project uses turnstile for process enforcement (currently **{enforce_mode}** mode).

**Before editing any code, start the appropriate process:**

{proc_table}

Use `process_status()` to check active instances. Use `process_transition()` to advance through states. The enforcement guard will remind you of your current state on every file edit.

States with `permissions.edit: false` restrict file edits until you transition to an implementation state. Follow the validation gates; they surface useful context."""

        click.echo(f"\n--- CLAUDE.md guidance (add to {claude_md}) ---")
        click.echo(guidance)
        click.echo("--- end guidance ---")
        click.echo(
            "\nAdd the above block to your project's CLAUDE.md, "
            "placing it where it fits best in the document structure."
        )

    if not created and already_has:
        click.echo("Everything already exists, nothing to do.")


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
@click.option("--dev", is_flag=True, default=False, help="Use dev mode (uv --directory).")
@click.option("--turnstile-dir", default=None, help="Path to turnstile repo (dev mode).")
@click.option("--repo", default=None, help="Git repo URL (uvx mode).")
@click.pass_context
def enforce_on(ctx: click.Context, dev: bool, turnstile_dir: str | None, repo: str | None) -> None:
    """Enable enforcement (blocks file mutations without an active process)."""
    root = Path(ctx.obj["project"]) if ctx.obj.get("project") else Path.cwd()
    update_registry_enforcement(root, "enforce")
    if dev:
        result = install_enforcement(
            root, "enforce", turnstile_dir=turnstile_dir or _find_turnstile_root()
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
    root = Path(ctx.obj["project"]) if ctx.obj.get("project") else Path.cwd()
    update_registry_enforcement(root, "monitor")
    if dev:
        result = install_enforcement(
            root, "monitor", turnstile_dir=turnstile_dir or _find_turnstile_root()
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
    root = Path(ctx.obj["project"]) if ctx.obj.get("project") else Path.cwd()
    update_registry_enforcement(root, "off")
    result = install_enforcement(root, "off")
    click.echo(f"Enforcement disabled: {result['message']}")


if __name__ == "__main__":
    cli()
