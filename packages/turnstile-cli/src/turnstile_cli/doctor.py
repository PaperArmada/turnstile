"""EXPERIMENTAL: acceptance-boundary configuration check.

A guarantee with a silent misconfiguration is worse than no guarantee
(docs/guarantee.md §4). `turnstile doctor` inspects everything checkable
locally and tells the operator exactly which screw is loose.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import click

from turnstile_core.definition.loader import load_registry

from turnstile_cli.context import project_root


def _check(results: list, status: str, name: str, detail: str) -> None:
    results.append((status, name, detail))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


@click.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """EXPERIMENTAL: check that the acceptance boundary is actually closed.

    Verifies the local half of the guarantee's trusted computing base:
    registry configuration, anchoring, operator key placement, ledger
    visibility to CI. Exits 1 on FAIL findings; WARN findings exit 0.
    """
    root = project_root(ctx)
    results: list[tuple[str, str, str]] = []

    # Git repository
    if (root / ".git").exists():
        _check(results, "OK", "git repository", str(root))
    else:
        _check(results, "FAIL", "git repository",
               "not a git repository — no acceptance boundary exists")

    # Registry + enforcement
    registry_path = root / ".processes" / "registry.yaml"
    if not registry_path.exists():
        _check(results, "FAIL", "registry",
               ".processes/registry.yaml not found — run 'turnstile init'")
        registry = None
    else:
        registry = load_registry(root)
        mode = registry.settings.enforcement
        status = "OK" if mode in ("monitor", "enforce") else "WARN"
        _check(results, status, "enforcement mode",
               mode + ("" if status == "OK" else
                       " — in-flight guidance is off (acceptance still applies)"))

    if registry is not None:
        v = registry.settings.verification

        # SHA recording (artifact binding)
        if v.record_git_sha:
            _check(results, "OK", "artifact binding",
                   "git SHAs recorded on history entries")
        else:
            _check(results, "WARN", "artifact binding",
                   "record_git_sha is off — verify cannot bind the trail "
                   "to commits")

        # Anchoring
        if not v.anchor_file and not v.anchor_command:
            _check(results, "WARN", "anchoring",
                   "no anchor configured — trail integrity will be "
                   "informational only (settings.verification.anchor_file "
                   "or anchor_command)")
        else:
            if v.anchor_file:
                anchor = Path(v.anchor_file).expanduser()
                if _inside(anchor, root):
                    _check(results, "FAIL", "anchoring",
                           f"anchor_file {anchor} is INSIDE the project — "
                           f"the agent can rewrite it; anchors prove "
                           f"nothing unless they live outside the agent's "
                           f"write domain")
                else:
                    _check(results, "OK", "anchoring",
                           f"anchor_file outside project: {anchor}")
            if v.anchor_command:
                _check(results, "OK", "anchoring (remote)",
                       f"anchor_command configured: {v.anchor_command[:60]}")

        # Operator key
        if v.signal_key_file:
            key = Path(v.signal_key_file).expanduser()
            if _inside(key, root):
                _check(results, "FAIL", "operator key",
                       f"signal_key_file {key} is INSIDE the project — the "
                       f"agent can read it and sign its own approvals")
            elif not key.exists():
                _check(results, "WARN", "operator key",
                       f"configured but missing: {key}")
            else:
                _check(results, "OK", "operator key",
                       f"outside project: {key}")
        else:
            _check(results, "WARN", "operator key",
                   "no signal_key_file configured — human-gated wait "
                   "states cannot be verified")

    # Ledger visibility to CI. Convention: active/ and log.txt are
    # ephemeral and ignored; completed/ and abandoned/ are the audit
    # record and must be visible for CI to verify.
    gitignore = root / ".gitignore"
    lines = (
        [ln.strip() for ln in gitignore.read_text().splitlines()]
        if gitignore.exists() else []
    )
    whole_ledger_ignored = any(
        ln in (".process-state", ".process-state/", ".process-state/*",
               "/.process-state", "/.process-state/")
        for ln in lines
    )
    if whole_ledger_ignored:
        _check(results, "WARN", "ledger visibility",
               "all of .process-state/ is gitignored — CI cannot see the "
               "trail; ignore only active/ and log.txt so the completed "
               "ledger is committed (docs/verification.md)")
    else:
        _check(results, "OK", "ledger visibility",
               "completed/abandoned ledger visible to version control")

    # Branch protection (best effort — needs gh)
    gh = shutil.which("gh")
    if gh:
        try:
            branch = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
                cwd=root, capture_output=True, text=True, timeout=10,
            ).stdout.strip().removeprefix("origin/") or "main"
            probe = subprocess.run(
                [gh, "api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection"],
                cwd=root, capture_output=True, text=True, timeout=15,
            )
            if probe.returncode == 0:
                _check(results, "OK", "branch protection",
                       f"'{branch}' is protected (verify that the "
                       f"turnstile check is required)")
            else:
                _check(results, "FAIL", "branch protection",
                       f"'{branch}' has no protection — work can land "
                       f"without verification")
        except Exception:
            _check(results, "WARN", "branch protection",
                   "could not query GitHub — ensure the default branch "
                   "requires the turnstile verify check")
    else:
        _check(results, "WARN", "branch protection",
               "gh CLI not available to check — ensure the default branch "
               "is protected and requires the turnstile verify check")

    # Render
    width = max(len(name) for _, name, _ in results)
    failed = False
    for status, name, detail in results:
        if status == "FAIL":
            failed = True
        click.echo(f"  {status:<5} {name:<{width}}  {detail}")

    click.echo()
    if failed:
        click.echo("The acceptance boundary is NOT closed. Fix FAIL items "
                   "before trusting verification.")
        raise SystemExit(1)
    warns = sum(1 for s, _, _ in results if s == "WARN")
    if warns:
        click.echo(f"Boundary usable with {warns} warning(s) — see above "
                   f"for what is not yet guaranteed.")
    else:
        click.echo("Acceptance boundary looks closed.")
