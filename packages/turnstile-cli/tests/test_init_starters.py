"""`turnstile init` must ship the curated starter pack (first-hour defect).

A fresh init historically created an empty .processes/, so `turnstile list`
reported "No process definitions found" while the README advertised built-in
starters. These tests pin that init installs the bundled pack and that the
bundle stays in sync with the repo's .processes/ source.
"""

from pathlib import Path

import yaml
from click.testing import CliRunner

from turnstile_cli.main import cli
from turnstile_cli.starters import STARTER_REGISTRY, STARTERS

REPO_ROOT = Path(__file__).resolve().parents[3]
PROCESSES_DIR = REPO_ROOT / ".processes"


def test_bundled_starters_match_repo_source():
    """The generated starters.py must match .processes/ (regenerate if this
    fails: `uv run python scripts/gen_starters.py`)."""
    repo_files = {
        p.name: p.read_text()
        for p in PROCESSES_DIR.glob("*.yaml")
        if p.name != "registry.yaml"
    }
    assert set(STARTERS) == set(repo_files), (
        "Bundled starter set differs from .processes/ — regenerate starters.py"
    )
    for name, content in repo_files.items():
        assert STARTERS[name] == content, f"Stale bundled content for {name}"


def test_registry_bundle_lists_every_starter():
    reg = yaml.safe_load(STARTER_REGISTRY.replace("__ENFORCEMENT__", "monitor"))
    local = set(reg.get("local", []))
    starter_names = {name[:-5] for name in STARTERS}  # strip .yaml
    assert starter_names <= local, "registry.local is missing starter(s)"
    assert "security-review" in local


def test_init_installs_starter_pack(tmp_path):
    """A fresh init writes every starter and a registry that registers them."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--project", str(tmp_path),
            "init", "--dev", "--turnstile-dir", str(REPO_ROOT),
            "--enforce", "monitor",
        ],
    )
    assert result.exit_code == 0, result.output

    proc_dir = tmp_path / ".processes"
    installed = {p.name for p in proc_dir.glob("*.yaml")} - {"registry.yaml"}
    assert installed == set(STARTERS)

    registry = yaml.safe_load((proc_dir / "registry.yaml").read_text())
    assert registry["settings"]["enforcement"] == "monitor"
    assert "security-review" in registry["local"]


def test_init_respects_enforce_mode(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--project", str(tmp_path),
            "init", "--dev", "--turnstile-dir", str(REPO_ROOT),
            "--enforce", "enforce",
        ],
    )
    assert result.exit_code == 0, result.output
    registry = yaml.safe_load(
        (tmp_path / ".processes" / "registry.yaml").read_text()
    )
    assert registry["settings"]["enforcement"] == "enforce"


def test_init_does_not_overwrite_existing_definitions(tmp_path):
    """Re-running init never clobbers an edited local definition."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    custom = (
        "name: my-custom-bug-fix\n"
        "version: 1.0.0\n"
        "states:\n"
        "  - id: start\n"
        "    type: initial\n"
        "    transitions: [done]\n"
        "  - id: done\n"
        "    type: terminal\n"
    )
    (proc_dir / "bug-fix.yaml").write_text(custom)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--project", str(tmp_path),
            "init", "--dev", "--turnstile-dir", str(REPO_ROOT),
            "--enforce", "monitor",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (proc_dir / "bug-fix.yaml").read_text() == custom
