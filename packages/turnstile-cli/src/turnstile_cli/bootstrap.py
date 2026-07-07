"""Project setup commands: init, update, init-package, git hooks."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import click

from turnstile_core.definition.loader import (
    discover_definitions_full,
    load_definition,
)
from turnstile_core.ops.githooks import generate_hook, install_hook, uninstall_hook
from turnstile_core.ops.guard import find_turnstile_root, install_enforcement
from turnstile_core.ops.scaffold import scaffold_package

from turnstile_cli.context import TURNSTILE_REPO_URL, project_root
from turnstile_cli.principles import PRINCIPLES


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
                    f"turnstile-mcp @ git+{repo_url}"
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


@click.group()
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
    root = project_root(ctx)
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
    root = project_root(ctx)
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


@click.command("init-package")
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


@click.command()
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
    root = project_root(ctx)
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
                turnstile_dir or find_turnstile_root()
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


@click.command()
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
    root = project_root(ctx)
    repo_url = repo or TURNSTILE_REPO_URL

    if dev and turnstile_dir is None:
        turnstile_dir = find_turnstile_root()

    created: list[str] = []
    proc_dir = root / ".processes"

    if not connect_only:
        # .processes/ directory
        if not proc_dir.exists():
            proc_dir.mkdir(parents=True)
            created.append(".processes/")

        # Design principles
        principles_dir = proc_dir / "principles"
        if not principles_dir.exists():
            principles_dir.mkdir(parents=True)
            written = 0
            for name, content in PRINCIPLES.items():
                (principles_dir / f"{name}.md").write_text(content)
                written += 1
            created.append(f".processes/principles/ ({written} files)")

        # registry.yaml
        registry_path = proc_dir / "registry.yaml"
        if not registry_path.exists():
            registry_path.write_text(
                f'version: "1.0"\n\nsettings:\n  enforcement: {enforce_mode}\n'
            )
            created.append(".processes/registry.yaml")
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

    # .gitignore additions. In-flight state and the text log are
    # ephemeral; the completed/abandoned ledger is deliberately NOT
    # ignored — it is the audit record acceptance verification reads
    # in CI (docs/verification.md).
    gitignore_path = root / ".gitignore"
    gitignore_entries = [
        ".mcp.json",
        ".claude/settings.json",
        ".process-state/active/",
        ".process-state/log.txt",
    ]
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
