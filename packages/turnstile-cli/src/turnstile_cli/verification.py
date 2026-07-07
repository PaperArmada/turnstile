"""EXPERIMENTAL: acceptance verification and operator approval commands."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
from pathlib import Path

import click
import yaml

from turnstile_core.definition.loader import load_registry
from turnstile_core.ops.verify import (
    AcceptancePolicy,
    latest_instance_id,
    sign_signal,
    verify_instance,
)

from turnstile_cli.context import get_engine, project_root

DEFAULT_KEY_PATH = "~/.turnstile/operator.key"


def _resolve_key_file(root: Path, explicit: str | None) -> Path | None:
    """Operator key resolution: flag > env > registry > default path.

    The key must live outside the agent's write domain — typically the
    operator's home directory — or signatures prove nothing.
    """
    candidates = [
        explicit,
        os.environ.get("TURNSTILE_SIGNAL_KEY_FILE"),
        load_registry(root).settings.verification.signal_key_file or None,
        DEFAULT_KEY_PATH,
    ]
    for c in candidates:
        if c:
            path = Path(c).expanduser()
            if path.exists():
                return path
    return None


@click.command()
@click.argument("instance_id", required=False, default=None)
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
    instance_id: str | None,
    policy_file: str,
    commit_range: str | None,
    as_json: bool,
) -> None:
    """EXPERIMENTAL: verify an instance against an acceptance policy.

    Exits 0 if the workflow was demonstrably followed, 1 otherwise.
    Designed to run as a required CI check at the acceptance boundary
    (see docs/guarantee.md). Everything reported PROVEN is recomputed
    here and does not trust the stored ledger.

    If INSTANCE_ID is omitted, verifies the most recently completed
    instance of the policy's process.
    """
    policy = AcceptancePolicy(**yaml.safe_load(Path(policy_file).read_text()))
    root = project_root(ctx)

    # Fill policy blanks from registry verification settings
    v = load_registry(root).settings.verification
    if not policy.anchor_file and v.anchor_file:
        policy.anchor_file = str(Path(v.anchor_file).expanduser())
    if not policy.signal_key_file and v.signal_key_file:
        policy.signal_key_file = str(Path(v.signal_key_file).expanduser())

    if instance_id is None:
        instance_id = latest_instance_id(root, policy.process)
        if instance_id is None:
            click.echo(
                f"No completed instance of '{policy.process}' found.", err=True
            )
            raise SystemExit(1)
        click.echo(f"(verifying latest completed instance: {instance_id})")

    report = asyncio.run(
        verify_instance(root, instance_id, policy, commit_range=commit_range)
    )

    if as_json:
        click.echo(json.dumps(report.model_dump(), indent=2))
    else:
        click.echo(report.render())

    raise SystemExit(0 if report.passed else 1)


@click.command()
@click.argument("instance_id")
@click.argument("signal_name")
@click.option(
    "--data", "-d", "data_pairs", multiple=True,
    help="Signal payload field as key=value (repeat for multiple).",
)
@click.option(
    "--to", "target_state", default=None,
    help="State to transition to after signal delivery.",
)
@click.option(
    "--key", "key_file", default=None, type=click.Path(),
    help=f"Operator key file (default: $TURNSTILE_SIGNAL_KEY_FILE, "
         f"registry settings, or {DEFAULT_KEY_PATH}).",
)
@click.option(
    "--as", "approved_by", default=None,
    help="Identity to record on the approval (default: current user).",
)
@click.pass_context
def approve(
    ctx: click.Context,
    instance_id: str,
    signal_name: str,
    data_pairs: tuple[str, ...],
    target_state: str | None,
    key_file: str | None,
    approved_by: str | None,
) -> None:
    """EXPERIMENTAL: deliver an operator-signed signal to a waiting instance.

    This is the human identity channel from docs/guarantee.md: run it
    yourself, from your own shell, with a key the agent cannot read.
    An agent delivering the same signal without the signature will
    fail acceptance verification on human-gated wait states.

    Example:

      turnstile approve a1b2c3 release_approval -d approved=true --to ship
    """
    root = project_root(ctx)
    key_path = _resolve_key_file(root, key_file)
    if key_path is None:
        click.echo(
            f"No operator key found. Create one (outside the agent's "
            f"write domain), e.g.:\n"
            f"  mkdir -p ~/.turnstile && "
            f"head -c 32 /dev/urandom > {DEFAULT_KEY_PATH}",
            err=True,
        )
        raise SystemExit(1)

    data: dict = {}
    for pair in data_pairs:
        if "=" not in pair:
            click.echo(f"Invalid data format: '{pair}' (expected key=value)", err=True)
            raise SystemExit(1)
        k, v = pair.split("=", 1)
        data[k] = v
    data["approved_by"] = approved_by or getpass.getuser()
    data["signature"] = sign_signal(
        key_path.read_bytes(), instance_id, signal_name, data
    )

    engine = get_engine(ctx.obj["project"])
    result = asyncio.run(
        engine.receive_signal(
            instance_id, signal_name, data,
            target_state=target_state,
            session_id=f"approve:{data['approved_by']}",
        )
    )

    click.echo(
        f"Signed signal '{signal_name}' delivered by {data['approved_by']}."
    )
    click.echo(f"  New state: {result['new_state']}")
    if result.get("available_transitions"):
        click.echo(f"  Next: {', '.join(result['available_transitions'])}")
    if result.get("summary"):
        click.echo(f"  Instance completed.")
