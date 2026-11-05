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

Later sections pin further gate fixes: command-valued parameters run as
shell syntax (#44), "Test suite must pass" gates check the suite's exit
status, and info checks report a failing command instead of an
unreachable fallback (#49).
"""

import re
from pathlib import Path

import pytest
import yaml

from turnstile_cli.starters import STARTERS
from turnstile_core.models import ProcessDefinition, ValidationRule
from turnstile_core.validator import run_command, run_validation

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
# Command-valued parameters run as the shell command the user wrote (#44)
# ---------------------------------------------------------------------------
#
# A bare ${test_command} is expanded as words only: an environment prefix,
# quotes, or a pipe in the value are not shell syntax, so a test invocation
# such as FOO="" pytest failed every test gate with 'FOO="": not found'.


def _test_gates(definition: dict, state_id: str, expect: str) -> list[str]:
    state = _state(definition, state_id)
    rules = []
    for hook in ("on_enter", "on_exit"):
        rules.extend((state.get(hook) or {}).get("validate") or [])
    return [
        r["command"]
        for r in rules
        if "test_command" in r["command"] and r.get("expect") == expect
    ]


TEST_GATES = [
    pytest.param(command, id=f"{process}/{state}/{expect}")
    for process, state in [
        ("bug-fix", "verify"),
        ("feature-development", "test"),
        ("feature-development", "understand"),
    ]
    for expect in ("exit_code(0)", "not_empty")
    for command in _test_gates(_starter(process), state, expect)
]

# (value, count it reports). Each value prints "<count> passed in 0.01s"
# only if the shell parsed it as the command it is: an assignment prefix
# plus quoting, and a pipeline that rewrites the count. Unparsed, the
# pipeline value echoes its own text, whose only count is 3.
SHELL_SYNTAX_TEST_COMMANDS = [
    pytest.param(
        """COUNT=3 sh -c 'echo "$COUNT passed in 0.01s"'""",
        "3",
        id="env-prefix-and-quotes",
    ),
    pytest.param("echo 3 passed in 0.01s | tr 3 7", "7", id="pipeline"),
    pytest.param("echo 5 passed in 0.01s", "5", id="plain"),
]


def test_test_gate_selection_finds_every_test_gate():
    assert len(TEST_GATES) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("test_command,count", SHELL_SYNTAX_TEST_COMMANDS)
@pytest.mark.parametrize("gate", TEST_GATES)
async def test_test_gate_runs_test_command_as_shell_syntax(
    tmp_path, gate, test_command, count
):
    expected = count if "grep -oP" in gate else f"{count} passed in 0.01s"
    output = await _run(gate, tmp_path, test_command=test_command)
    assert output == expected


# ---------------------------------------------------------------------------
# "Test suite must pass" gates refuse a red suite
# ---------------------------------------------------------------------------
#
# The gate matched contains("passed") on the summary line, and a red pytest
# summary such as "1 failed, 3 passed" contains "passed".


def _suite_gate(process: str, state_id: str) -> ValidationRule:
    state = _state(_starter(process), state_id)
    rules = [
        r
        for r in state["on_exit"]["validate"]
        if r.get("message") == "Test suite must pass"
    ]
    assert len(rules) == 1
    return ValidationRule(**rules[0])


SUITE_GATES = [
    pytest.param(_suite_gate("bug-fix", "verify"), id="bug-fix/verify"),
    pytest.param(
        _suite_gate("feature-development", "test"), id="feature-development/test"
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", SUITE_GATES)
async def test_suite_gate_refuses_a_red_suite_whose_summary_says_passed(
    tmp_path, gate
):
    red = "echo '=== 1 failed, 3 passed in 0.10s ==='; exit 1"
    result = await run_validation(gate, {"test_command": red}, tmp_path)
    assert not result.passed
    assert "1 failed, 3 passed" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", SUITE_GATES)
async def test_suite_gate_accepts_a_green_suite(tmp_path, gate):
    green = "echo '=== 3 passed in 0.10s ==='"
    result = await run_validation(gate, {"test_command": green}, tmp_path)
    assert result.passed
    assert "3 passed" in result.output


# eval of a blank command exits 0, so the gate itself cannot tell "no test
# ran" from a green suite. The engine gives a blank test_command the
# declared default instead (#51); this pins that for the shipped starters.
@pytest.mark.parametrize("process", ["bug-fix", "feature-development"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_test_command_starts_with_the_default(tmp_path, process, blank):
    from turnstile_core.engine import Engine

    processes = tmp_path / ".processes"
    processes.mkdir()
    (processes / f"{process}.yaml").write_text(STARTERS[f"{process}.yaml"])
    required = "bug_summary" if process == "bug-fix" else "feature_name"
    params = {"test_command": blank, required: "x"}
    started = Engine(tmp_path).start(process, params)
    assert started["parameters"]["test_command"] == "pytest"


@pytest.mark.asyncio
async def test_bug_fix_with_a_blank_test_command_cannot_skip_the_suite(tmp_path):
    """End to end: the suite gate runs the default, which finds no tests
    in an empty directory (or is not installed) and refuses `done`."""
    from turnstile_core.engine import Engine

    processes = tmp_path / ".processes"
    processes.mkdir()
    (processes / "bug-fix.yaml").write_text(STARTERS["bug-fix.yaml"])
    engine = Engine(tmp_path)
    iid = engine.start("bug-fix", {"bug_summary": "x", "test_command": ""})[
        "instance_id"
    ]
    for state in ("reproduce", "diagnose", "fix", "verify"):
        assert (await engine.transition(iid, state)).success
    result = await engine.transition(iid, "done")
    assert not result.success


# ---------------------------------------------------------------------------
# A fallback after a pipeline must be reachable (#49)
# ---------------------------------------------------------------------------
#
# In `cmd | tail -1 || echo X` the fallback never runs: a pipeline's status
# is its last stage's, and tail (like head, wc, sed, ...) exits 0 on empty
# input. Info checks built this way recorded an empty, failed check exactly
# when the fallback should have answered, and the baseline test check passed
# on a test_command that could not run, because the shell's own "not found"
# line satisfied not_empty. The checks now branch on the producing command's
# own exit status: its failure is reported as a failed check showing the
# error, and only a successful empty result prints the fallback.
#
# The sweep below is a regex over the common shapes, not a shell parser: a
# fallback reached through a stage it does not list, or through constructs
# it does not model, is not caught.

_ALWAYS_ZERO_FILTER = (
    r"(?:tail|head|wc|tr|sort|uniq|cut|sed|awk|cat|tee|jq|nl|paste|fold)"
)
_DEAD_FALLBACK_RE = re.compile(
    # a pipe into a filter, allowing an env prefix or a path
    r"\|\s*(?:\w+=\S*\s+)*(?:\S*/)?" + _ALWAYS_ZERO_FILTER + r"\b"
    # its arguments: quoted strings and >& / &> redirections stay inside
    r"(?:[^|;&'\"]|[0-9]?>&|&>|'[^']*'|\"[^\"]*\")*"
    # optionally closing a { ...; } group, then the fallback operator
    r"\s*(?:;\s*\}\s*)?\|\|"
)


def _has_dead_fallback(command: str) -> bool:
    return bool(_DEAD_FALLBACK_RE.search(command))


@pytest.mark.parametrize(
    "command",
    [
        "git diff --stat HEAD 2>/dev/null | tail -1 || echo 'no changes'",
        "cmd --json 2>/dev/null | head -1 || echo '{}'",
        "cmd 2>/dev/null | head -5 || echo 'no data'",
        "(cmd | tail -1) || echo x",
        "ls | wc -l || echo 0",
        "cmd | tail -1 2>&1 || echo x",
        "{ cmd | tail -1; } || echo x",
        "cmd | tr -d '|' || echo x",
        "cmd | /usr/bin/tail -1 || echo x",
        "cmd | LC_ALL=C sort || echo x",
        "cmd | head -1 | sed s/a/b/ || echo x",
    ],
)
def test_dead_fallback_scanner_rejects_unreachable_fallbacks(command):
    """Canary: a broken scanner would green the sweep silently."""
    assert _has_dead_fallback(command)


@pytest.mark.parametrize(
    "command",
    [
        "git diff --stat HEAD 2>/dev/null | tail -1 | grep . || echo 'no changes'",
        (
            "out=$(git diff --stat HEAD 2>&1) || { printf '%s\\n' \"$out\"; exit 1; }; "
            "printf '%s\\n' \"${out:-no changes}\" | tail -1"
        ),
        'test -n "$(cmd | tail -1)" || echo none',
        "ls .process-state/active/*.json 2>/dev/null | wc -l | tr -d ' '",
        'test -f x && echo exists || echo missing',
        "git log --oneline -5 2>/dev/null || echo 'not a git repo'",
        (
            "out=$(eval \"${test_command}\" 2>&1); status=$?; "
            "printf '%s\\n' \"$out\" | tail -1; exit $status"
        ),
    ],
)
def test_dead_fallback_scanner_accepts_reachable_forms(command):
    assert not _has_dead_fallback(command)


def test_no_command_has_an_unreachable_pipeline_fallback():
    offenders = [
        (source, command)
        for source, command in _all_commands()
        if _has_dead_fallback(command)
    ]
    assert offenders == [], (
        "a fallback after '| tail' (head, sed, ...) never runs; branch on "
        f"the producing command's exit status instead: {offenders}"
    )


def _rule(process: str, state_id: str, message: str) -> ValidationRule:
    state = _state(_starter(process), state_id)
    rules = [
        r
        for hook in ("on_enter", "on_exit")
        for r in (state.get(hook) or {}).get("validate") or []
        if r.get("message") == message
    ]
    assert len(rules) == 1, f"{process}/{state_id}: {message!r}"
    return ValidationRule(**rules[0])


# (rule, fallback text). The code-review rules diff base_branch...branch;
# the others diff the working tree against HEAD.
CHANGE_SUMMARY_RULES = [
    pytest.param(
        _rule("feature-development", "test", "Changes since last commit"),
        "no changes",
        id="feature-development/test",
    ),
    pytest.param(
        _rule("bug-fix", "diagnose", "Files changed so far (investigation artifacts)"),
        "no changes",
        id="bug-fix/diagnose",
    ),
    pytest.param(
        _rule("spike", "write_up", "Changes made during investigation"),
        "no changes",
        id="spike/write_up",
    ),
    pytest.param(
        _rule(
            "code-review",
            "survey",
            "Change summary (files changed, insertions, deletions)",
        ),
        "no diff available",
        id="code-review/survey",
    ),
    pytest.param(
        _rule(
            "code-review",
            "request_changes",
            "Current state of changes (has author addressed feedback?)",
        ),
        "no diff",
        id="code-review/request_changes",
    ),
]


@pytest.fixture
def clean_repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("rule,fallback", CHANGE_SUMMARY_RULES)
async def test_change_summary_reports_its_fallback_when_nothing_changed(
    clean_repo, rule, fallback
):
    params = {"base_branch": "main", "branch": "main"}
    result = await run_validation(rule, params, clean_repo)
    assert result.passed
    assert result.output == fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("rule,fallback", CHANGE_SUMMARY_RULES)
async def test_change_summary_reports_the_diff_when_something_changed(
    clean_repo, rule, fallback
):
    _git(clean_repo, "checkout", "-q", "-b", "feat")
    (clean_repo / "a.txt").write_text("two\n")
    _git(clean_repo, "commit", "-q", "-am", "change")
    (clean_repo / "a.txt").write_text("three\n")
    params = {"base_branch": "main", "branch": "feat"}
    result = await run_validation(rule, params, clean_repo)
    assert result.passed
    assert result.output.startswith("1 file changed")


@pytest.mark.asyncio
@pytest.mark.parametrize("rule,fallback", CHANGE_SUMMARY_RULES)
async def test_change_summary_reports_a_git_failure_not_its_fallback(
    tmp_path, rule, fallback
):
    params = {"base_branch": "main", "branch": "main"}
    result = await run_validation(rule, params, tmp_path)  # not a repository
    assert not result.passed
    assert "not a git repository" in result.output.lower()
    assert "\n" not in result.output  # the first line, not a usage dump


BASELINE_RULE = _rule(
    "feature-development",
    "understand",
    "Current test suite status (baseline before changes)",
)


@pytest.mark.asyncio
async def test_baseline_fails_when_test_command_cannot_run(tmp_path):
    result = await run_validation(
        BASELINE_RULE, {"test_command": "turnstile_no_such_command_49"}, tmp_path
    )
    assert not result.passed
    assert "not found" in result.output


@pytest.mark.asyncio
async def test_baseline_fails_on_a_red_suite_and_shows_its_summary(tmp_path):
    red = "echo '=== 1 failed, 3 passed in 0.10s ==='; exit 1"
    result = await run_validation(BASELINE_RULE, {"test_command": red}, tmp_path)
    assert not result.passed
    assert "1 failed, 3 passed" in result.output


@pytest.mark.asyncio
async def test_baseline_passes_on_a_green_suite(tmp_path):
    green = "echo '=== 3 passed in 0.10s ==='"
    result = await run_validation(BASELINE_RULE, {"test_command": green}, tmp_path)
    assert result.passed
    assert result.output == "=== 3 passed in 0.10s ==="


# Parameters whose value is, by contract, a shell command the instance
# starter supplies. Only these may be eval'd: re-parsing a data-valued
# parameter as shell syntax (eval, sh -c, '.', a pipe into sh) is the
# injection the env-var passing exists to prevent.
COMMAND_PARAMETERS = {"test_command"}
EXAMPLES_DIR = REPO_ROOT / "examples"
_EVAL_RE = re.compile(r"\beval\b")
# The canonical form must end the eval's arguments, or eval would join a
# following expansion into the code it parses.
_CANONICAL_EVAL_RE = re.compile(
    r'\beval "\$\{(\w+)\}"(?=\s*(?:$|[|;&)]|[0-9]*>))'
)
# $name, ${name} and ${name:-default} all expand the parameter.
_EXPANSION_RE = re.compile(r"\$\{?(\w+)")
_REPARSE_RE = re.compile(
    r'\b(?:sh|bash|dash|zsh)\s+-c\s+"?\$\{?\w'
    r'|(?:^|[;&|(]\s*)(?:\.|source)\s+"?\$\{?\w'
    r'|\$\{?\w+\}?"?\s*\|\s*(?:sh|bash|dash|zsh)\b'
)


def _all_commands():
    for name, content in sorted(STARTERS.items()):
        for command in _iter_commands(yaml.safe_load(content)):
            yield f"bundle:{name}", command
    for directory in (PROCESSES_DIR, EXAMPLES_DIR):
        for path in sorted(directory.glob("*.yaml")):
            for command in _iter_commands(yaml.safe_load(path.read_text())):
                yield f"{directory.name}/{path.name}", command


def _evals_only_command_parameters(command: str) -> bool:
    evaluated = _CANONICAL_EVAL_RE.findall(command)
    return (
        len(_EVAL_RE.findall(command)) == len(evaluated)
        and set(evaluated) <= COMMAND_PARAMETERS
        and not _REPARSE_RE.search(command)
    )


def _evals_every_command_parameter(command: str) -> bool:
    expanded = [p for p in _EXPANSION_RE.findall(command) if p in COMMAND_PARAMETERS]
    return len(_CANONICAL_EVAL_RE.findall(command)) == len(expanded)


@pytest.mark.parametrize(
    "command",
    [
        'eval "${test_command}" 2>&1 | tail -1',
        'eval "${test_command}" >/dev/null 2>&1',
        'eval "${test_command}"',
        'test "${output_file}" = "none" && echo skip',
    ],
)
def test_eval_sweep_accepts_safe_forms(command):
    assert _evals_only_command_parameters(command)
    assert _evals_every_command_parameter(command)


@pytest.mark.parametrize(
    "command",
    [
        'eval "${output_file}"',
        'eval "$test_command"',
        "eval ${test_command}",
        'eval "${test_command}" "${output_file}"',
        'eval "${test_command}" ${output_file}',
        'sh -c "${output_file}"',
        'bash -c "$output_file"',
        '. "${output_file}"',
        'true; source "${output_file}"',
        'echo "${output_file}" | sh',
    ],
)
def test_eval_sweep_rejects_reparsing_forms(command):
    """Canary: each form re-parses a value the sweep must not allow."""
    assert not _evals_only_command_parameters(command)


@pytest.mark.parametrize(
    "command",
    [
        "${test_command} 2>&1 | tail -1",
        "$test_command 2>&1 | tail -1",
        "${test_command:-pytest} 2>&1 | tail -1",
    ],
)
def test_eval_sweep_rejects_unevaluated_command_parameters(command):
    """Canary: each form runs a command parameter without parsing it."""
    assert not _evals_every_command_parameter(command)


def test_only_command_parameters_are_evaluated():
    offenders = [
        (source, command)
        for source, command in _all_commands()
        if not _evals_only_command_parameters(command)
    ]
    assert offenders == [], (
        f'eval only {sorted(COMMAND_PARAMETERS)}, as eval "${{name}}": {offenders}'
    )


def test_command_parameters_are_always_evaluated():
    offenders = [
        (source, command)
        for source, command in _all_commands()
        if not _evals_every_command_parameter(command)
    ]
    assert offenders == [], (
        f'expand command parameters with eval "${{name}}": {offenders}'
    )


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


# ---------------------------------------------------------------------------
# Retreats are never gated (#45): exit gates are scoped to the forward move
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "process, state_id, forward, back",
    [
        ("bug-fix", "verify", "done", "fix"),
        ("feature-development", "test", "review", "implement"),
        ("create-process", "draft", "review", "understand"),
        ("readme-update", "update", "verify", "audit"),
    ],
)
def test_exit_gates_guard_the_forward_move_only(process, state_id, forward, back):
    defn = ProcessDefinition(**_starter(process))
    gates = defn.get_state(state_id).on_exit.validations
    assert [g for g in gates if g.applies_to(forward)] != []
    assert [g for g in gates if g.applies_to(back)] == []


def test_dry_run_cli_shows_the_scope(tmp_path):
    from click.testing import CliRunner

    from turnstile_cli.main import cli

    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "bug-fix.yaml").write_text(STARTERS["bug-fix.yaml"])
    result = CliRunner().invoke(
        cli, ["--project", str(tmp_path), "dry-run", "bug-fix"]
    )
    assert result.exit_code == 0, result.output
    assert "[error] Test suite must pass (only to done)" in result.output


# create-process: the overwrite guard sits on start's exit, so returning
# from draft to understand with the draft present is not refused.


def test_create_process_understand_has_no_blocking_entry_gate():
    defn = ProcessDefinition(**_starter("create-process"))
    entry = defn.get_state("understand").on_enter.validations
    assert [g for g in entry if g.severity.value == "error"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("draft_exists, passes", [(False, True), (True, False)])
async def test_create_process_overwrite_guard_runs_on_leaving_start(
    tmp_path, draft_exists, passes
):
    defn = ProcessDefinition(**_starter("create-process"))
    (guard,) = defn.get_state("start").on_exit.validations
    if draft_exists:
        (tmp_path / ".processes").mkdir()
        (tmp_path / ".processes" / "demo.yaml").write_text("name: demo\n")
    result = await run_validation(guard, {"name": "demo"}, tmp_path)
    assert result.passed is passes


# ---------------------------------------------------------------------------
# release: the tag is created inside the tag state
# ---------------------------------------------------------------------------
#
# Entering tag refused any existing tag, so tagging early (to check the
# tagged build before pushing) meant skipping the gate. Entry now refuses
# only a tag on another commit; leaving for done requires the tag at HEAD.


def _git(cwd: Path, *args: str) -> None:
    import subprocess

    # No hooks: the developer's global commit hooks must not judge fixtures.
    config = ["-c", "user.name=t", "-c", "user.email=t@t"]
    config += ["-c", "core.hooksPath=/dev/null"]
    subprocess.run(
        ["git", *config, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def release_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "first")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "second")
    return tmp_path


def _release_gate(hook: str) -> ValidationRule:
    state = ProcessDefinition(**_starter("release")).get_state("tag")
    (gate,) = [
        g for g in getattr(state, hook).validations if g.severity.value == "error"
    ]
    return gate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tag_args, passes",
    [
        pytest.param([], True, id="no-tag"),
        pytest.param(["v1.0.0"], True, id="lightweight-at-head"),
        pytest.param(["-a", "v1.0.0", "-m", "v1.0.0"], True, id="annotated-at-head"),
        pytest.param(["v1.0.0", "HEAD~1"], False, id="on-another-commit"),
    ],
)
async def test_release_tag_entry_refuses_only_a_tag_elsewhere(
    release_repo, tag_args, passes
):
    if tag_args:
        _git(release_repo, "tag", *tag_args)
    result = await run_validation(
        _release_gate("on_enter"), {"version": "1.0.0"}, release_repo
    )
    assert result.passed is passes, result.output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tag_args, passes",
    [
        pytest.param([], False, id="no-tag"),
        pytest.param(["v1.0.0"], True, id="lightweight-at-head"),
        pytest.param(["-a", "v1.0.0", "-m", "v1.0.0"], True, id="annotated-at-head"),
        pytest.param(["v1.0.0", "HEAD~1"], False, id="on-another-commit"),
        pytest.param(["v1.0.1"], False, id="other-version"),
    ],
)
async def test_release_tag_exit_requires_the_tag_at_head(
    release_repo, tag_args, passes
):
    if tag_args:
        _git(release_repo, "tag", *tag_args)
    result = await run_validation(
        _release_gate("on_exit"), {"version": "1.0.0"}, release_repo
    )
    assert result.passed is passes, result.output


def test_release_tag_exit_gate_does_not_block_the_retreat():
    gate = _release_gate("on_exit")
    assert gate.applies_to("done")
    assert not gate.applies_to("validate")
