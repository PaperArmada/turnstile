"""Tests for the `turnstile gc` CLI command."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

from turnstile_cli.main import cli
from turnstile_core.engine import Engine

SIMPLE_YAML = (
    "name: simple\n"
    "version: 1.0.0\n"
    "description: minimal test process\n"
    "states:\n"
    "  - id: start\n"
    "    type: initial\n"
    "    transitions: [done]\n"
    "  - id: done\n"
    "    type: terminal\n"
)


def _project_with_stale_instance(tmp_path: Path) -> str:
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "simple.yaml").write_text(SIMPLE_YAML)
    engine = Engine(tmp_path)
    inst = engine.start("simple")
    stamp = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    active_dir = tmp_path / ".process-state" / "active"
    for path in active_dir.glob("*.json"):
        data = json.loads(path.read_text())
        if data["instance_id"] == inst["instance_id"]:
            data["updated_at"] = stamp
            path.write_text(json.dumps(data))
    return inst["instance_id"]


def test_gc_dry_run_lists_but_keeps(tmp_path):
    iid = _project_with_stale_instance(tmp_path)
    result = CliRunner().invoke(
        cli, ["--project", str(tmp_path), "gc", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Would abandon 1:" in result.output
    assert iid in result.output
    assert len(Engine(tmp_path)._store.list_active()) == 1


def test_gc_abandons(tmp_path):
    iid = _project_with_stale_instance(tmp_path)
    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "gc"])
    assert result.exit_code == 0, result.output
    assert "Abandoned 1:" in result.output
    assert iid in result.output
    assert Engine(tmp_path)._store.list_active() == []


def test_gc_nothing_stale(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "simple.yaml").write_text(SIMPLE_YAML)
    Engine(tmp_path).start("simple")
    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "gc"])
    assert result.exit_code == 0, result.output
    assert "Nothing stale." in result.output


def test_gc_rejects_non_positive_override(tmp_path):
    _project_with_stale_instance(tmp_path)
    result = CliRunner().invoke(
        cli, ["--project", str(tmp_path), "gc", "--older-than-days", "0"]
    )
    assert result.exit_code == 0, result.output
    assert "must be positive" in result.output
    assert len(Engine(tmp_path)._store.list_active()) == 1
