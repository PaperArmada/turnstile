"""CLI error handling for unknown process names (GH #40, bug B).

`dry-run`, `info`, and `graph` take a user-supplied process name and must
fail cleanly when it cannot be resolved: one readable error on stderr,
exit code 1, no traceback. Historically ProcessNotFoundError escaped as a
raw traceback. Additionally, when `.processes/<name>.yaml` exists but is
not listed in registry.local (so non-empty-local discovery skips it), the
error must hint at adding the name under `local:` in
.processes/registry.yaml.

These tests run the CLI in a subprocess rather than via CliRunner:
the contract under test is what a terminal user sees (real stderr, real
exit code, presence/absence of a traceback), which CliRunner masks by
catching exceptions in-process.
"""

import subprocess
import sys
from pathlib import Path

import pytest

MINIMAL_DEF = (
    "name: {name}\n"
    "version: 1.0.0\n"
    "description: minimal test process\n"
    "states:\n"
    "  - id: start\n"
    "    type: initial\n"
    "    transitions: [done]\n"
    "  - id: done\n"
    "    type: terminal\n"
)

REGISTRY = (
    "version: '1.0'\n"
    "local:\n"
    "- registered-proc\n"
    "settings:\n"
    "  enforcement: 'off'\n"
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Project with a non-empty registry.local (auto-discovery off) and
    one orphan definition file that the registry does not list."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "registered-proc.yaml").write_text(
        MINIMAL_DEF.format(name="registered-proc")
    )
    (proc_dir / "orphan-proc.yaml").write_text(
        MINIMAL_DEF.format(name="orphan-proc")
    )
    (proc_dir / "registry.yaml").write_text(REGISTRY)
    return tmp_path


def _run_cli(project_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from turnstile_cli.main import cli; cli()",
            "--project",
            str(project_root),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("command", ["dry-run", "info", "graph"])
def test_unknown_process_name_fails_cleanly(project, command):
    """A name matching nothing yields exit 1 and a readable one-line
    error on stderr, never a raw traceback."""
    result = _run_cli(project, command, "no-such-proc")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, (
        f"raw traceback leaked to the user:\n{result.stderr}"
    )
    assert "Traceback" not in result.stdout
    assert "no-such-proc" in result.stderr, (
        f"error should name the unknown process; stderr was: {result.stderr!r}"
    )


def test_ghost_registry_entry_fails_cleanly_without_false_hint(tmp_path):
    """registry.local lists 'ghost' but ghost.yaml does not exist: any
    name-based command fails cleanly, naming the broken registry entry,
    and does NOT emit a registration hint about the queried name.

    Target mutants: (a) only wrapping the name-lookup call, so the
    discovery-time ProcessNotFoundError raised while loading the registry
    escapes as a raw traceback; (b) keying the hint solely on the queried
    name's file existing, printing a misleading 'add - anything under
    local:' for an error that is actually about 'ghost'.
    """
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nlocal:\n- ghost\nsettings:\n  enforcement: 'off'\n"
    )

    result = _run_cli(tmp_path, "dry-run", "anything")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, (
        f"raw traceback leaked to the user:\n{result.stderr}"
    )
    assert "Traceback" not in result.stdout
    assert "ghost" in result.stderr, (
        f"error should name the broken registry entry; stderr was: "
        f"{result.stderr!r}"
    )
    assert "anything" not in result.stderr, (
        f"no hint about the queried name belongs here; stderr was: "
        f"{result.stderr!r}"
    )


def test_registered_file_with_mismatched_internal_name_no_false_hint(tmp_path):
    """registry.local lists 'foo' and foo.yaml exists, but the file's
    internal name is 'bar': `dry-run foo` fails without the
    'not registered' hint (foo IS registered) and instead points at the
    internal "name:" field as the thing being addressed.

    Target mutant: keying the hint on file existence alone. foo.yaml
    exists and 'foo' is absent from the *discovered names*, so a naive
    check prints 'add - foo under local:', sending the user to re-add an
    entry that is already there instead of at the actual mismatch.
    """
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "foo.yaml").write_text(MINIMAL_DEF.format(name="bar"))
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nlocal:\n- foo\nsettings:\n  enforcement: 'off'\n"
    )

    result = _run_cli(tmp_path, "dry-run", "foo")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, (
        f"raw traceback leaked to the user:\n{result.stderr}"
    )
    assert "foo" in result.stderr
    assert "not registered" not in result.stderr, (
        f"false registration hint; stderr was: {result.stderr!r}"
    )
    assert '"name:"' in result.stderr, (
        f"error should point at the internal name field; stderr was: "
        f"{result.stderr!r}"
    )


@pytest.mark.parametrize("command", ["dry-run", "info", "graph"])
def test_unregistered_local_file_error_hints_at_registry(project, command):
    """When .processes/<name>.yaml exists but is missing from
    registry.local, the error explains how to register it."""
    result = _run_cli(project, command, "orphan-proc")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, (
        f"raw traceback leaked to the user:\n{result.stderr}"
    )
    assert "orphan-proc" in result.stderr
    assert "local:" in result.stderr, (
        "error should hint at adding the name under 'local:' in the "
        f"registry; stderr was: {result.stderr!r}"
    )
    assert "registry.yaml" in result.stderr, (
        f"error should point at .processes/registry.yaml; stderr was: "
        f"{result.stderr!r}"
    )
