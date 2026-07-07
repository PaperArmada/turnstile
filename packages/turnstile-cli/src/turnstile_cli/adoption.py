"""EXPERIMENTAL: adopt an existing CLAUDE.md / rules file into a process."""

from __future__ import annotations

from pathlib import Path

import click
import yaml

from turnstile_core.ops.adopt import (
    KNOWN_SOURCES,
    analyze,
    build_definition,
    build_policy,
    find_source,
    render_adoption_report,
)

from turnstile_cli.context import project_root


@click.command()
@click.argument("source", required=False, default=None,
                type=click.Path(exists=True))
@click.option(
    "--name", "-n", default="adopted-workflow",
    help="Name for the drafted process.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Print the report and drafts without writing files.",
)
@click.option("--force", is_flag=True, help="Overwrite existing drafts.")
@click.pass_context
def adopt(
    ctx: click.Context,
    source: str | None,
    name: str,
    dry_run: bool,
    force: bool,
) -> None:
    """EXPERIMENTAL: turn a CLAUDE.md / rules file into a draft process.

    Extracts the workflow-shaped content from an existing agent
    instructions file — verification commands become gates,
    prohibitions become command restrictions — and drafts a valid
    process definition plus acceptance policy. Everything that needs
    human judgment is listed in the adoption report with source line
    references; behavioral guidance is left in the markdown where it
    belongs.

    SOURCE defaults to the first of: CLAUDE.md, AGENTS.md,
    .cursorrules, .cursor/rules, GEMINI.md, or the Copilot
    instructions file.
    """
    root = project_root(ctx)

    if source:
        source_path = Path(source)
    else:
        source_path = find_source(root)
        if source_path is None:
            click.echo(
                "No rules file found. Looked for: "
                + ", ".join(KNOWN_SOURCES),
                err=True,
            )
            raise SystemExit(1)

    text = source_path.read_text()
    adoption = analyze(text, source_path.name)

    definition = build_definition(name, adoption)
    policy = build_policy(name, adoption)

    definition_path = root / ".processes" / f"{name}.yaml"
    policy_path = root / ".processes" / "policies" / f"{name}.yaml"

    report = render_adoption_report(
        adoption, name,
        str(definition_path.relative_to(root)),
        str(policy_path.relative_to(root)),
    )

    if dry_run:
        click.echo(report)
        click.echo("--- draft definition ---")
        click.echo(yaml.dump(definition, sort_keys=False))
        click.echo("--- draft policy ---")
        click.echo(yaml.dump(policy, sort_keys=False))
        return

    for path in (definition_path, policy_path):
        if path.exists() and not force:
            click.echo(
                f"{path} already exists. Use --force to overwrite, "
                f"or --name for a different name.",
                err=True,
            )
            raise SystemExit(1)

    definition_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    definition_path.write_text(yaml.dump(definition, sort_keys=False))
    policy_path.write_text(yaml.dump(policy, sort_keys=False))

    click.echo(report)
