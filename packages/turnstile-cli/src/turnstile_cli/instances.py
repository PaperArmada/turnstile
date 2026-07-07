"""Commands about process instances: status, active, check-completed,
analytics."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import click

from turnstile_cli.context import get_engine


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


@click.command()
@click.option("--all", "-a", "show_all", is_flag=True, help="Show all details.")
@click.pass_context
def status(ctx: click.Context, show_all: bool) -> None:
    """Show active process instances."""
    engine = get_engine(ctx.obj["project"])
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


@click.command()
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
    engine = get_engine(ctx.obj["project"])
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


@click.command("check-completed")
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
    engine = get_engine(ctx.obj["project"])

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


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def analytics(ctx: click.Context, as_json: bool) -> None:
    """Show process analytics from archived instances."""
    engine = get_engine(ctx.obj["project"])
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
