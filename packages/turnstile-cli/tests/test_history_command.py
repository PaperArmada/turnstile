"""`turnstile history <instance_id>`: human review of a recorded trajectory.

The command renders the instance envelope (name, id, status, current state,
parameters), every transition with gate results and metadata, and the
override log, searching active and archived instances alike. `--json` emits
the full engine trajectory record verbatim for machine consumption.

Rendering tests use CliRunner (in-process output is the contract). The
unknown-instance test runs in a subprocess like test_cli_errors.py, because
its contract is what a terminal user sees: real stderr, real exit code, no
traceback.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from turnstile_core.engine import Engine
from turnstile_cli.main import cli

HIST_DEF = """\
name: histproc
version: 1.0.0
description: history rendering test process
parameters:
  - name: task
    required: true
states:
  - id: start
    type: initial
    transitions: [work]
  - id: work
    transitions: [review]
    on_exit:
      validate:
        - command: "echo checked"
          expect: not_empty
          message: "Work verified"
  - id: review
    transitions: [done, advisory]
  - id: advisory
    transitions: [done]
    on_enter:
      validate:
        - command: "echo oops-detail"
          expect: contains("never-there")
          severity: warning
          message: "Advisory gate"
  - id: done
    type: terminal
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "histproc.yaml").write_text(HIST_DEF)
    return tmp_path


@pytest.fixture
def engine(project: Path) -> Engine:
    return Engine(project)


def _invoke(project: Path, *args: str):
    return CliRunner().invoke(cli, ["--project", str(project), *args])


class TestRenderedTrajectory:
    @pytest.fixture
    def transitioned(self, engine: Engine) -> str:
        """Instance advanced start -> work (with metadata) -> review."""
        iid = engine.start("histproc", {"task": "demo"})["instance_id"]
        asyncio.run(engine.transition(iid, "work", metadata={"pr": "42"}))
        asyncio.run(engine.transition(iid, "review"))
        return iid

    def test_header_names_process_instance_status_and_state(
        self, project, transitioned
    ):
        result = _invoke(project, "history", transitioned)
        assert result.exit_code == 0, result.output
        assert f"histproc [{transitioned}] active @ review" in result.output

    def test_parameters_rendered(self, project, transitioned):
        result = _invoke(project, "history", transitioned)
        assert "parameters: task=demo" in result.output

    def test_transitions_listed_with_count(self, project, transitioned):
        result = _invoke(project, "history", transitioned)
        assert "Transitions (2):" in result.output
        assert "start -> work" in result.output
        assert "work -> review" in result.output

    def test_passing_gate_marked_ok_with_message(self, project, transitioned):
        result = _invoke(project, "history", transitioned)
        assert "[ok] Work verified" in result.output

    def test_transition_metadata_rendered(self, project, transitioned):
        result = _invoke(project, "history", transitioned)
        assert "pr: 42" in result.output

    def test_no_overrides_section_when_nothing_skipped(
        self, project, transitioned
    ):
        result = _invoke(project, "history", transitioned)
        assert "Overrides" not in result.output

    def test_failed_advisory_gate_shows_severity_and_output(
        self, project, engine
    ):
        """A failing warning-severity gate does not block the transition;
        the record shows the severity as the mark plus the first line of
        the gate's output."""
        iid = engine.start("histproc", {"task": "demo"})["instance_id"]
        asyncio.run(engine.transition(iid, "work"))
        asyncio.run(engine.transition(iid, "review"))
        asyncio.run(engine.transition(iid, "advisory"))

        result = _invoke(project, "history", iid)
        assert result.exit_code == 0, result.output
        assert "[warning] Advisory gate" in result.output
        assert "oops-detail" in result.output


class TestOverrideRendering:
    def test_skip_reason_rendered_in_overrides_section(self, project, engine):
        iid = engine.start("histproc", {"task": "demo"})["instance_id"]
        asyncio.run(engine.skip(iid, "review", "gates run by hand"))

        result = _invoke(project, "history", iid)
        assert result.exit_code == 0, result.output
        assert "Overrides (1):" in result.output
        assert "start -> review" in result.output
        assert "gates run by hand" in result.output

    def test_override_attributed_to_recorded_session(self, project, engine):
        """When the skip carried a session id, the override line names it
        as the actor instead of falling back to 'unknown'."""
        iid = engine.start("histproc", {"task": "demo"})["instance_id"]
        asyncio.run(
            engine.skip(
                iid, "review", "gates run by hand", session_id="sess-123"
            )
        )

        result = _invoke(project, "history", iid)
        assert result.exit_code == 0, result.output
        assert "by sess-123: gates run by hand" in result.output
        assert "by unknown" not in result.output

    def test_override_without_session_attributed_to_unknown(
        self, project, engine
    ):
        """No recorded session id renders the honest fallback actor."""
        iid = engine.start("histproc", {"task": "demo"})["instance_id"]
        asyncio.run(engine.skip(iid, "review", "no session context"))

        result = _invoke(project, "history", iid)
        assert result.exit_code == 0, result.output
        assert "by unknown: no session context" in result.output


class TestSanitization:
    """Every rendered field is writable by the agent under review, so raw
    control bytes must never reach the terminal: a carriage return plus an
    ANSI erase-line could redraw the audit view a human is reading."""

    def test_control_characters_render_escaped_not_raw(
        self, project, engine
    ):
        iid = engine.start("histproc", {"task": "demo"})["instance_id"]
        asyncio.run(
            engine.transition(
                iid, "work", metadata={"note": "evil\r\x1b[2Kspoofed"}
            )
        )

        result = _invoke(project, "history", iid)
        assert result.exit_code == 0, result.output
        # Raw control bytes must be absent from stdout entirely
        assert "\r" not in result.output
        assert "\x1b" not in result.output
        # ... and visible as literal escapes instead
        assert "evil\\r\\x1b[2Kspoofed" in result.output

    def test_nested_metadata_rendered_as_json(self, project, engine):
        iid = engine.start("histproc", {"task": "demo"})["instance_id"]
        asyncio.run(
            engine.transition(
                iid, "work", metadata={"payload": {"a": [1, 2]}}
            )
        )

        result = _invoke(project, "history", iid)
        assert result.exit_code == 0, result.output
        assert 'payload: {"a": [1, 2]}' in result.output
        assert "{'a': [1, 2]}" not in result.output


class TestEdgeInstances:
    def test_fresh_instance_reports_no_transitions(self, project, engine):
        iid = engine.start("histproc", {"task": "demo"})["instance_id"]

        result = _invoke(project, "history", iid)
        assert result.exit_code == 0, result.output
        assert "No transitions recorded." in result.output
        assert "Transitions" not in result.output
        assert "Overrides" not in result.output

    def test_completed_instance_still_reviewable(self, project, engine):
        iid = engine.start("histproc", {"task": "demo"})["instance_id"]
        asyncio.run(engine.transition(iid, "work"))
        asyncio.run(engine.transition(iid, "review"))
        asyncio.run(engine.transition(iid, "done"))

        result = _invoke(project, "history", iid)
        assert result.exit_code == 0, result.output
        assert f"histproc [{iid}] completed @ done" in result.output
        assert "Transitions (3):" in result.output

    def test_abandoned_instance_still_reviewable(self, project, engine):
        iid = engine.start("histproc", {"task": "demo"})["instance_id"]
        asyncio.run(engine.transition(iid, "work"))
        engine.abandon(iid, "requirements changed")

        result = _invoke(project, "history", iid)
        assert result.exit_code == 0, result.output
        assert f"histproc [{iid}] abandoned @ work" in result.output
        assert "Transitions (1):" in result.output


class TestJsonOutput:
    def test_json_emits_the_full_trajectory_record(self, project, engine):
        iid = engine.start("histproc", {"task": "demo"})["instance_id"]
        asyncio.run(engine.transition(iid, "work", metadata={"pr": "42"}))
        asyncio.run(engine.skip(iid, "done", "fast-tracked"))

        result = _invoke(project, "history", iid, "--json")
        assert result.exit_code == 0, result.output
        record = json.loads(result.output)
        assert record == engine.trajectory(iid)
        # Spot-check the record is the full thing, not a summary
        assert record["instance_id"] == iid
        assert len(record["transitions"]) == 2
        assert record["overrides"][0]["reason"] == "fast-tracked"


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


def test_unknown_instance_fails_cleanly(project):
    """An id matching nothing yields exit 1 and a readable error on stderr
    naming the id, never a raw traceback."""
    result = _run_cli(project, "history", "no-such-id")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, (
        f"raw traceback leaked to the user:\n{result.stderr}"
    )
    assert "Traceback" not in result.stdout
    assert "no-such-id" in result.stderr, (
        f"error should name the unknown instance; stderr was: {result.stderr!r}"
    )
    assert "Error:" in result.stderr


def test_corrupt_state_file_fails_cleanly(project, engine):
    """A torn/truncated state file for the queried instance yields exit 1
    and a readable Error line, never a JSONDecodeError traceback."""
    iid = engine.start("histproc", {"task": "demo"})["instance_id"]
    [state_file] = (project / ".process-state" / "active").glob(
        f"*{iid}*.json"
    )
    state_file.write_text(state_file.read_text()[:20])

    result = _run_cli(project, "history", iid)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, (
        f"raw traceback leaked to the user:\n{result.stderr}"
    )
    assert "Traceback" not in result.stdout
    assert "Error:" in result.stderr, (
        f"expected a readable Error line; stderr was: {result.stderr!r}"
    )
