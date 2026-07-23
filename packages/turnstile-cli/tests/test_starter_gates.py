"""Starter-pack gate commands must actually expand their parameters.

Regression pins for the gate-expansion fixes: six gates across five starters
wrapped ${var} in shell single quotes, so the variable never expanded. The
issue-linkage gate reported the literal string 'linked to ${issue_id}', and
every skip-when-none / file-exists check compared the literal '${output_file}'
against "none" (the skip branch was dead code) and then file-tested that same
literal. The fix is the double-quoted form: parameter values reach the shell
as environment variables and the shell expands "${var}" itself. A follow-up
fix added '--' before grep patterns so a parameter value beginning with '-'
reads as a (missing) filename instead of a grep option.

These tests read the gate commands from the shipped bundle
(turnstile_cli.starters.STARTERS), never from hardcoded copies, so a
regression in the YAML itself fails the behavioral tests. A structural sweep
additionally rejects the single-quoted-placeholder anti-pattern anywhere a
command appears, in both the bundle and the repo .processes/ source.
"""

from pathlib import Path

import pytest
import yaml

from turnstile_cli.starters import STARTERS
from turnstile_core.validator import run_command

REPO_ROOT = Path(__file__).resolve().parents[3]
PROCESSES_DIR = REPO_ROOT / ".processes"


# ---------------------------------------------------------------------------
# Helpers: locate gate commands inside shipped definitions
# ---------------------------------------------------------------------------


def _starter(process_name: str) -> dict:
    """Parse a bundled starter definition."""
    return yaml.safe_load(STARTERS[f"{process_name}.yaml"])


def _state(definition: dict, state_id: str) -> dict:
    matches = [s for s in definition["states"] if s["id"] == state_id]
    assert matches, f"state '{state_id}' not found in {definition['name']}"
    return matches[0]


def _gate_command(definition: dict, state_id: str, placeholder: str) -> str:
    """The single validation command in a state referencing ${placeholder}.

    Selecting by placeholder reference (not rule index) keeps the tests
    stable across unrelated gate additions; asserting exactly one match
    keeps the selection unambiguous.
    """
    state = _state(definition, state_id)
    rules = []
    for hook in ("on_enter", "on_exit"):
        rules.extend((state.get(hook) or {}).get("validate") or [])
    needle = "${" + placeholder + "}"
    commands = [r["command"] for r in rules if needle in r.get("command", "")]
    assert len(commands) == 1, (
        f"expected exactly one gate referencing {needle} in "
        f"{definition['name']}/{state_id}, found {len(commands)}"
    )
    return commands[0]


async def _run(command: str, cwd: Path, **parameters: str) -> str:
    output, _exit_code = await run_command(command, cwd, parameters=parameters)
    return output


# ---------------------------------------------------------------------------
# feature-development: issue-linkage gate expands issue_id
# ---------------------------------------------------------------------------


ISSUE_GATE = _gate_command(_starter("feature-development"), "understand", "issue_id")


@pytest.mark.asyncio
async def test_issue_gate_reports_no_linkage_for_sentinel_none(tmp_path):
    output = await _run(ISSUE_GATE, tmp_path, issue_id="none")
    assert "no issue linked" in output


@pytest.mark.asyncio
async def test_issue_gate_expands_the_issue_id_into_its_report(tmp_path):
    output = await _run(ISSUE_GATE, tmp_path, issue_id="GH-123")
    assert "linked to GH-123" in output
    assert "${issue_id}" not in output


# ---------------------------------------------------------------------------
# spike: write_up output_file gate (skip-when-none, file-exists)
# ---------------------------------------------------------------------------


SPIKE_GATE = _gate_command(_starter("spike"), "write_up", "output_file")


@pytest.mark.asyncio
async def test_spike_output_gate_skips_when_no_output_file_requested(tmp_path):
    output = await _run(SPIKE_GATE, tmp_path, output_file="none")
    assert output == "skip"


@pytest.mark.asyncio
async def test_spike_output_gate_sees_the_actual_findings_file(tmp_path):
    findings = tmp_path / "findings.md"
    findings.write_text("# Findings\n")
    output = await _run(SPIKE_GATE, tmp_path, output_file=str(findings))
    assert output == "exists"


@pytest.mark.asyncio
async def test_spike_output_gate_flags_a_missing_findings_file(tmp_path):
    output = await _run(SPIKE_GATE, tmp_path, output_file=str(tmp_path / "absent.md"))
    assert output == "missing"


# ---------------------------------------------------------------------------
# scientific-method: hypothesize grep gate and conclude file-exists gate
# ---------------------------------------------------------------------------


HYPOTHESIS_GATE = _gate_command(_starter("scientific-method"), "hypothesize", "output_file")
CONCLUDE_GATE = _gate_command(_starter("scientific-method"), "conclude", "output_file")


@pytest.mark.asyncio
async def test_hypothesis_gate_skips_when_no_output_file_requested(tmp_path):
    output = await _run(HYPOTHESIS_GATE, tmp_path, output_file="none")
    assert output == "skip"


@pytest.mark.asyncio
async def test_hypothesis_gate_finds_a_documented_hypothesis(tmp_path):
    doc = tmp_path / "investigation.md"
    doc.write_text("## My Hypothesis\nCaching is the bottleneck.\n")
    output = await _run(HYPOTHESIS_GATE, tmp_path, output_file=str(doc))
    assert output == "found"


@pytest.mark.asyncio
async def test_hypothesis_gate_flags_a_missing_investigation_file(tmp_path):
    output = await _run(HYPOTHESIS_GATE, tmp_path, output_file=str(tmp_path / "absent.md"))
    assert output == "missing"


@pytest.mark.asyncio
async def test_conclude_gate_skips_when_no_output_file_requested(tmp_path):
    output = await _run(CONCLUDE_GATE, tmp_path, output_file="none")
    assert output == "skip"


@pytest.mark.asyncio
async def test_conclude_gate_sees_the_actual_conclusions_file(tmp_path):
    doc = tmp_path / "conclusions.md"
    doc.write_text("Supported.\n")
    output = await _run(CONCLUDE_GATE, tmp_path, output_file=str(doc))
    assert output == "exists"


@pytest.mark.asyncio
async def test_conclude_gate_flags_a_missing_conclusions_file(tmp_path):
    output = await _run(CONCLUDE_GATE, tmp_path, output_file=str(tmp_path / "absent.md"))
    assert output == "missing"


# ---------------------------------------------------------------------------
# decision-record: evaluate and decide grep gates
# ---------------------------------------------------------------------------


OPTIONS_GATE = _gate_command(_starter("decision-record"), "evaluate", "output_file")
DECISION_GATE = _gate_command(_starter("decision-record"), "decide", "output_file")


@pytest.mark.asyncio
async def test_options_gate_skips_when_no_output_file_requested(tmp_path):
    output = await _run(OPTIONS_GATE, tmp_path, output_file="none")
    assert output == "skip"


@pytest.mark.asyncio
async def test_options_gate_finds_documented_options(tmp_path):
    doc = tmp_path / "decision.md"
    doc.write_text("## Options considered\n1. Buy\n2. Build\n")
    output = await _run(OPTIONS_GATE, tmp_path, output_file=str(doc))
    assert output == "found"


@pytest.mark.asyncio
async def test_options_gate_flags_a_missing_record_file(tmp_path):
    output = await _run(OPTIONS_GATE, tmp_path, output_file=str(tmp_path / "absent.md"))
    assert output == "missing"


@pytest.mark.asyncio
async def test_decision_gate_skips_when_no_output_file_requested(tmp_path):
    output = await _run(DECISION_GATE, tmp_path, output_file="none")
    assert output == "skip"


@pytest.mark.asyncio
async def test_decision_gate_finds_a_documented_rationale(tmp_path):
    doc = tmp_path / "decision.md"
    doc.write_text("We chose the managed provider for lower ops load.\n")
    output = await _run(DECISION_GATE, tmp_path, output_file=str(doc))
    assert output == "found"


@pytest.mark.asyncio
async def test_decision_gate_flags_a_missing_record_file(tmp_path):
    output = await _run(DECISION_GATE, tmp_path, output_file=str(tmp_path / "absent.md"))
    assert output == "missing"


# ---------------------------------------------------------------------------
# peer-review: prepare artifact_path gate (test -e admits files and dirs)
# ---------------------------------------------------------------------------


ARTIFACT_GATE = _gate_command(_starter("peer-review"), "prepare", "artifact_path")


@pytest.mark.asyncio
async def test_artifact_gate_skips_when_no_path_specified(tmp_path):
    output = await _run(ARTIFACT_GATE, tmp_path, artifact_path="none")
    assert output == "skip"


@pytest.mark.asyncio
async def test_artifact_gate_sees_an_artifact_file(tmp_path):
    artifact = tmp_path / "design.md"
    artifact.write_text("# Design\n")
    output = await _run(ARTIFACT_GATE, tmp_path, artifact_path=str(artifact))
    assert output == "exists"


@pytest.mark.asyncio
async def test_artifact_gate_accepts_a_directory_artifact(tmp_path):
    artifact_dir = tmp_path / "auth-module"
    artifact_dir.mkdir()
    output = await _run(ARTIFACT_GATE, tmp_path, artifact_path=str(artifact_dir))
    assert output == "exists"


@pytest.mark.asyncio
async def test_artifact_gate_flags_a_missing_artifact(tmp_path):
    output = await _run(ARTIFACT_GATE, tmp_path, artifact_path=str(tmp_path / "gone"))
    assert output == "missing"


# ---------------------------------------------------------------------------
# Option injection: a parameter beginning with '-' must read as a filename
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_gate_treats_dash_prefixed_value_as_filename_not_flag(tmp_path):
    """output_file='-r' must report missing, not recurse the working tree.

    Before the '--' fix, grep parsed '-r' as its recursive flag and searched
    the cwd; a planted match made the gate false-pass with 'found'. The
    planted file below is what makes this test discriminating: the pre-fix
    command finds it, the fixed command reports the file '-r' as missing.
    """
    planted_dir = tmp_path / "notes"
    planted_dir.mkdir()
    (planted_dir / "scratch.md").write_text("an option worth considering\n")
    output = await _run(OPTIONS_GATE, tmp_path, output_file="-r")
    assert output == "missing"


# ---------------------------------------------------------------------------
# Domain display gates expand ${domain}
# ---------------------------------------------------------------------------


DR_DOMAIN_GATE = _gate_command(_starter("decision-record"), "frame", "domain")
SM_DOMAIN_GATE = _gate_command(_starter("scientific-method"), "observe", "domain")


@pytest.mark.asyncio
async def test_decision_domain_gate_displays_the_actual_domain(tmp_path):
    output = await _run(DR_DOMAIN_GATE, tmp_path, domain="vendor selection")
    assert output == "Domain: vendor selection"


@pytest.mark.asyncio
async def test_observation_domain_gate_displays_the_actual_domain(tmp_path):
    output = await _run(SM_DOMAIN_GATE, tmp_path, domain="market research")
    assert output == "Domain: market research"


# ---------------------------------------------------------------------------
# Structural sweep: the single-quoted-placeholder anti-pattern must not
# appear in any command, anywhere in the pack
# ---------------------------------------------------------------------------


def _has_single_quoted_placeholder(command: str) -> bool:
    """True if '${' occurs inside a shell single-quoted region.

    Minimal POSIX-shell quote tracking: single quotes have no escapes
    inside; backslash escapes the next character outside single quotes.
    A placeholder inside single quotes is never expanded by the shell,
    which is exactly the bug this sweep rejects.
    """
    in_single = in_double = False
    i = 0
    while i < len(command):
        char = command[i]
        if in_single:
            if char == "'":
                in_single = False
            elif command.startswith("${", i):
                return True
        elif in_double:
            if char == "\\":
                i += 1
            elif char == '"':
                in_double = False
        else:
            if char == "\\":
                i += 1
            elif char == "'":
                in_single = True
            elif char == '"':
                in_double = True
        i += 1
    return False


def _iter_commands(node):
    """Yield every value stored under a 'command' key, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "command" and isinstance(value, str):
                yield value
            else:
                yield from _iter_commands(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_commands(item)


def test_sweep_scanner_recognizes_the_historical_bug_form():
    """Canary for the sweep itself: a broken scanner would green the sweep
    silently. These are the literal pre-fix and post-fix gate forms."""
    pre_fix = (
        "test '${output_file}' = 'none' && echo skip || "
        "(grep -qi 'hypothes' '${output_file}' 2>/dev/null "
        "&& echo found || echo missing)"
    )
    post_fix = (
        'test "${output_file}" = "none" && echo skip || '
        '(grep -qi -- hypothes "${output_file}" 2>/dev/null '
        "&& echo found || echo missing)"
    )
    assert _has_single_quoted_placeholder(pre_fix)
    assert not _has_single_quoted_placeholder(post_fix)


def test_no_bundled_starter_single_quotes_a_placeholder():
    offenders = [
        (name, command)
        for name, content in sorted(STARTERS.items())
        for command in _iter_commands(yaml.safe_load(content))
        if _has_single_quoted_placeholder(command)
    ]
    assert offenders == [], (
        "single-quoted ${var} never expands; use the double-quoted "
        f"env-var form instead: {offenders}"
    )


def test_no_repo_process_definition_single_quotes_a_placeholder():
    offenders = [
        (path.name, command)
        for path in sorted(PROCESSES_DIR.glob("*.yaml"))
        for command in _iter_commands(yaml.safe_load(path.read_text()))
        if _has_single_quoted_placeholder(command)
    ]
    assert offenders == [], (
        "single-quoted ${var} never expands; use the double-quoted "
        f"env-var form instead: {offenders}"
    )
