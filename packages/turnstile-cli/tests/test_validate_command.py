"""`turnstile validate` static-analysis output (GH #30).

A loadable definition with structural smells (unreachable states,
dead-end sinks) stays valid (exit 0) but gets "  Warning: ..." lines
after the Valid block. A clean definition prints no Warning lines. A
dispatch state whose `immediate` names a missing state now fails at load,
so validate reports Invalid on stderr with exit 1.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from turnstile_cli.main import cli

CLEAN_DEF = (
    "name: clean\n"
    "version: '1.0.0'\n"
    "description: clean linear process\n"
    "states:\n"
    "  - id: start\n"
    "    type: initial\n"
    "    transitions: [work]\n"
    "  - id: work\n"
    "    transitions: [done]\n"
    "  - id: done\n"
    "    type: terminal\n"
)

SMELLY_DEF = (
    "name: smelly\n"
    "version: '1.0.0'\n"
    "description: loads fine, warns on analysis\n"
    "states:\n"
    "  - id: start\n"
    "    type: initial\n"
    "    transitions: [sink, done]\n"
    "  - id: island\n"
    "    transitions: [done]\n"
    "  - id: sink\n"
    "  - id: done\n"
    "    type: terminal\n"
)

BAD_IMMEDIATE_DEF = (
    "name: bad-dispatch\n"
    "version: '1.0.0'\n"
    "description: dispatch immediate typo\n"
    "states:\n"
    "  - id: start\n"
    "    type: initial\n"
    "    transitions: [fire]\n"
    "  - id: fire\n"
    "    type: dispatch\n"
    "    process: child\n"
    "    immediate: nowhere\n"
    "  - id: done\n"
    "    type: terminal\n"
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


def test_smelly_definition_is_valid_with_warning_lines(tmp_path):
    path = _write(tmp_path, "smelly.yaml", SMELLY_DEF)
    result = CliRunner().invoke(cli, ["validate", str(path)])
    assert result.exit_code == 0, result.output
    assert "Valid: smelly v1.0.0" in result.output
    assert (
        "  Warning: State 'island' is unreachable from the initial state"
        in result.output
    )
    assert (
        "  Warning: State 'sink' cannot reach any terminal state (dead end)"
        in result.output
    )


def test_clean_definition_prints_no_warning_lines(tmp_path):
    path = _write(tmp_path, "clean.yaml", CLEAN_DEF)
    result = CliRunner().invoke(cli, ["validate", str(path)])
    assert result.exit_code == 0, result.output
    assert "Valid: clean v1.0.0" in result.output
    assert "Warning" not in result.output


def test_dispatch_immediate_typo_is_a_load_error(tmp_path):
    path = _write(tmp_path, "bad-dispatch.yaml", BAD_IMMEDIATE_DEF)
    result = CliRunner().invoke(cli, ["validate", str(path)])
    assert result.exit_code == 1
    assert "Invalid:" in result.stderr
    assert (
        "Dispatch state 'fire' immediate target 'nowhere' "
        "references unknown state" in result.stderr
    )
