"""`turnstile init` must not gitignore files the project tracks (GH #43).

Init appended `.mcp.json` and `.claude/settings.json` to `.gitignore`
unconditionally. Projects that commit `.claude/settings.json` (shared hooks)
ended up with an ignore rule contradicting their tracked files, which bites
on the next clone or `git rm --cached`.

Contract pinned here:
- uvx mode (the default) ignores only `.process-state/`; its generated
  `.mcp.json` and `.claude/settings.json` pin a release tag and are
  portable, so the project decides whether to commit them.
- dev mode also ignores `.mcp.json` and `.claude/settings.json`, which
  embed the absolute turnstile checkout path.
- A path git already tracks is never added, in either mode, and init says
  so. In dev mode it additionally warns that the tracked file now holds a
  machine-specific path.
- Outside a git repository the tracked check fails open to the plain append.
- An entry already present in `.gitignore` is not duplicated.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from click.testing import CliRunner

from turnstile_cli.main import cli

REPO_ROOT = Path(__file__).resolve().parents[3]

def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    )


def _git_repo(root: Path) -> None:
    _git(root, "init", "-q")


def _track_files(root: Path, *rel_paths: str) -> None:
    """Stage files so git tracks them; `ls-files` reads the index, so no
    commit is needed (and commit hooks on the dev machine stay out of it)."""
    for rel in rel_paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    # -f: a global excludes file may ignore .mcp.json on the dev machine.
    _git(root, "add", "-f", "--", *rel_paths)


def _run_init(root: Path, *, dev: bool):
    args = ["--project", str(root), "init", "--enforce", "monitor"]
    if dev:
        args += ["--dev", "--turnstile-dir", str(REPO_ROOT)]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    return result


def _ignore_lines(root: Path) -> list[str]:
    path = root / ".gitignore"
    if not path.exists():
        return []
    return [ln for ln in path.read_text().splitlines() if ln.strip()]


def test_uvx_mode_ignores_only_runtime_state(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    _run_init(tmp_path, dev=False)
    assert _ignore_lines(tmp_path) == [".process-state/"]


def test_dev_mode_ignores_machine_specific_files(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    _run_init(tmp_path, dev=True)
    assert _ignore_lines(tmp_path) == [
        ".mcp.json", ".claude/settings.json", ".process-state/",
    ]


def test_tracked_files_are_never_ignored_in_dev_mode(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    _track_files(tmp_path, ".claude/settings.json", ".mcp.json")
    result = _run_init(tmp_path, dev=True)

    assert _ignore_lines(tmp_path) == [".process-state/"]
    assert "not ignoring .mcp.json, .claude/settings.json (tracked by git)" in (
        result.output
    )
    assert "do not commit them as-is" in result.output
    check = subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "core.excludesFile=/dev/null",
         "check-ignore", "-v", "--no-index", ".claude/settings.json", ".mcp.json"],
        capture_output=True, text=True,
    )
    # exit 1 + empty stdout: no path matched any ignore rule.
    assert (check.returncode, check.stdout) == (1, ""), check


def test_tracked_settings_only_skips_just_that_entry(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    _track_files(tmp_path, ".claude/settings.json")
    _run_init(tmp_path, dev=True)
    assert _ignore_lines(tmp_path) == [".mcp.json", ".process-state/"]


def test_tracked_check_does_not_warn_in_uvx_mode(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    _track_files(tmp_path, ".claude/settings.json", ".mcp.json")
    result = _run_init(tmp_path, dev=False)
    assert _ignore_lines(tmp_path) == [".process-state/"]
    assert "not ignoring" not in result.output
    assert "do not commit" not in result.output


def test_outside_a_git_repo_falls_back_to_plain_append(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}\n")
    result = _run_init(tmp_path, dev=True)
    assert _ignore_lines(tmp_path) == [
        ".mcp.json", ".claude/settings.json", ".process-state/",
    ]
    assert "not ignoring" not in result.output


def test_existing_entries_are_not_duplicated(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("node_modules/\n.process-state/\n")
    result = _run_init(tmp_path, dev=False)
    assert _ignore_lines(tmp_path) == ["node_modules/", ".process-state/"]
    assert ".gitignore" not in result.output
