"""Tests for command-level state permissions (run / allow_commands /
deny_commands) and their enforcement through the guard's Bash routing."""

import json

import pytest
import yaml

from turnstile_core.definition.model import StatePermissions
from turnstile_core.ops.enforcement import (
    _command_permitted,
    check_enforcement,
)
from turnstile_core.runtime.engine import Engine

PROCESS = {
    "name": "release-flow",
    "version": "1.0.0",
    "states": [
        {"id": "start", "type": "initial", "transitions": ["prepare"]},
        {
            "id": "prepare",
            "description": "Build and test; publishing is not yet allowed",
            "permissions": {
                "deny_commands": ["git push*", "*deploy*"],
            },
            "transitions": ["locked", "shipped"],
        },
        {
            "id": "locked",
            "description": "Frozen for audit: read-only commands only",
            "permissions": {
                "edit": False,
                "allow_commands": ["git status*", "git log*", "ls*"],
            },
            "transitions": ["shipped"],
        },
        {"id": "shipped", "type": "terminal"},
    ],
}


@pytest.fixture
def project(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "release-flow.yaml").write_text(yaml.dump(PROCESS))
    (proc_dir / "registry.yaml").write_text(
        yaml.dump({"version": "1.0", "settings": {"enforcement": "enforce"}})
    )
    return tmp_path


async def instance_in(project, state):
    engine = Engine(project)
    iid = engine.start("release-flow")["instance_id"]
    await engine.transition(iid, "prepare")
    if state == "locked":
        await engine.transition(iid, "locked")
    return iid


class TestModel:
    def test_defaults_permissive(self):
        perms = StatePermissions()
        assert perms.run is True
        assert perms.allow_commands == []
        assert perms.deny_commands == []

    def test_yaml_shape(self):
        perms = StatePermissions(
            run=True,
            deny_commands=["git push*"],
            allow_commands=["git status*"],
        )
        assert perms.deny_commands == ["git push*"]


class TestCommandPatterns:
    def test_no_restrictions_allows(self):
        assert _command_permitted(StatePermissions(), "rm -rf /tmp/x") == ""

    def test_deny_pattern_blocks(self):
        perms = StatePermissions(deny_commands=["git push*"])
        assert "denied pattern" in _command_permitted(perms, "git push origin main")
        assert _command_permitted(perms, "git status") == ""

    def test_substring_glob(self):
        perms = StatePermissions(deny_commands=["*deploy*"])
        assert "denied pattern" in _command_permitted(perms, "make deploy-prod")

    def test_allowlist_blocks_unlisted(self):
        perms = StatePermissions(allow_commands=["git status*", "ls*"])
        assert _command_permitted(perms, "git status --short") == ""
        assert "not in allowed" in _command_permitted(perms, "git push origin main")

    def test_deny_wins_over_allow(self):
        perms = StatePermissions(
            allow_commands=["git*"], deny_commands=["git push*"]
        )
        assert "denied pattern" in _command_permitted(perms, "git push")


class TestEnforcement:
    async def test_denied_command_denied(self, project):
        await instance_in(project, "prepare")
        result = check_enforcement(project, action="run", command="git push origin main")
        assert result.decision == "deny"
        assert "denied pattern 'git push*'" in result.reason

    async def test_ordinary_command_allowed(self, project):
        await instance_in(project, "prepare")
        result = check_enforcement(project, action="run", command="pytest -q")
        assert result.decision == "allow"

    async def test_allowlist_state_blocks_mutations(self, project):
        await instance_in(project, "locked")
        result = check_enforcement(project, action="run", command="rm -rf src")
        assert result.decision == "deny"
        assert "not in allowed" in result.reason

    async def test_allowlist_state_permits_reads(self, project):
        await instance_in(project, "locked")
        result = check_enforcement(project, action="run", command="git log --oneline")
        assert result.decision == "allow"

    async def test_monitor_mode_warns_instead(self, project):
        registry = project / ".processes" / "registry.yaml"
        registry.write_text(
            yaml.dump({"version": "1.0", "settings": {"enforcement": "monitor"}})
        )
        await instance_in(project, "prepare")
        result = check_enforcement(project, action="run", command="git push")
        assert result.decision == "warn"

    async def test_run_false_blocks_everything(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        (proc_dir / "p.yaml").write_text(yaml.dump({
            "name": "p",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["frozen"]},
                {
                    "id": "frozen",
                    "permissions": {"run": False},
                    "transitions": ["done"],
                },
                {"id": "done", "type": "terminal"},
            ],
        }))
        (proc_dir / "registry.yaml").write_text(
            yaml.dump({"version": "1.0", "settings": {"enforcement": "enforce"}})
        )
        engine = Engine(tmp_path)
        iid = engine.start("p")["instance_id"]
        await engine.transition(iid, "frozen")
        result = check_enforcement(tmp_path, action="run", command="echo hi")
        assert result.decision == "deny"
        assert "does not allow run" in result.reason

    async def test_edit_permission_untouched_by_command_rules(self, project):
        """Command patterns must not bleed into edit decisions."""
        await instance_in(project, "prepare")
        result = check_enforcement(project, action="edit", file_path="src/x.py")
        assert result.decision == "allow"


class TestGuardRouting:
    async def test_bash_tool_routed_to_run(self, project, monkeypatch, capsys):
        from turnstile_core.ops import guard

        await instance_in(project, "prepare")
        payload = json.dumps({
            "cwd": str(project),
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"},
        })
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        guard.run_guard()
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "git push*" in out["hookSpecificOutput"]["permissionDecisionReason"]

    async def test_edit_tool_still_routed_to_edit(self, project, monkeypatch, capsys):
        from turnstile_core.ops import guard

        await instance_in(project, "locked")  # edit: false in locked
        payload = json.dumps({
            "cwd": str(project),
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/x.py"},
        })
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        guard.run_guard()
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_bash_allowed_when_no_restrictions(self, project, monkeypatch, capsys):
        from turnstile_core.ops import guard

        await instance_in(project, "prepare")
        payload = json.dumps({
            "cwd": str(project),
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
        })
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        guard.run_guard()
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
