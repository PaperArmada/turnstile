"""Turnstile CLI entry point.

Commands live in modules grouped by concern, mirroring the core layers:

- definitions.py: validate, list, info, graph, dry-run, schema
- instances.py:   status, active, check-completed, analytics
- bootstrap.py:   init, update, init-package, git hooks
- enforcement.py: guard, enforce {status,on,monitor,off}
"""

from __future__ import annotations

import os

import click

from turnstile_cli import bootstrap, definitions, enforcement, instances


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


# Definitions
cli.add_command(definitions.validate)
cli.add_command(definitions.list_processes)
cli.add_command(definitions.info)
cli.add_command(definitions.graph)
cli.add_command(definitions.dry_run)
cli.add_command(definitions.schema)

# Instances
cli.add_command(instances.status)
cli.add_command(instances.active)
cli.add_command(instances.check_completed)
cli.add_command(instances.analytics)

# Project setup
cli.add_command(bootstrap.init)
cli.add_command(bootstrap.update)
cli.add_command(bootstrap.init_package)
cli.add_command(bootstrap.hooks)

# Enforcement
cli.add_command(enforcement.guard)
cli.add_command(enforcement.enforce)


if __name__ == "__main__":
    cli()
