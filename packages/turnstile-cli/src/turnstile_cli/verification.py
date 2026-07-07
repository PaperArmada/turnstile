"""EXPERIMENTAL: acceptance-time conformance verification command."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
import yaml

from turnstile_core.ops.verify import AcceptancePolicy, verify_instance

from turnstile_cli.context import project_root


@click.command()
@click.argument("instance_id")
@click.option(
    "--policy", "policy_file", required=True, type=click.Path(exists=True),
    help="Acceptance policy YAML.",
)
@click.option(
    "--range", "commit_range", default=None,
    help="Git commit range to bind (e.g. main..HEAD).",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def verify(
    ctx: click.Context,
    instance_id: str,
    policy_file: str,
    commit_range: str | None,
    as_json: bool,
) -> None:
    """EXPERIMENTAL: verify an instance against an acceptance policy.

    Exits 0 if the workflow was demonstrably followed, 1 otherwise.
    Designed to run as a required CI check at the acceptance boundary
    (see docs/guarantee.md). Everything reported PROVEN is recomputed
    here and does not trust the stored ledger.
    """
    policy = AcceptancePolicy(**yaml.safe_load(Path(policy_file).read_text()))
    root = project_root(ctx)

    report = asyncio.run(
        verify_instance(root, instance_id, policy, commit_range=commit_range)
    )

    if as_json:
        click.echo(json.dumps(report.model_dump(), indent=2))
    else:
        click.echo(report.render())

    raise SystemExit(0 if report.passed else 1)
