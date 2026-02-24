"""Tests for turnstile_core.guard."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from turnstile_core.engine import Engine
from turnstile_core.guard import (
    check_enforcement,
    generate_hook_config,
    install_enforcement,
    update_registry_enforcement,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _setup_project(tmp_path, enforcement="off"):
    """Set up a project with a process definition and registry."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")

    if enforcement != "off":
        registry = {
            "version": "1.0",
            "settings": {"enforcement": enforcement},
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

    return Engine(tmp_path)


class TestCheckEnforcement:
    def test_off_mode_allows(self, tmp_path):
        _setup_project(tmp_path, enforcement="off")
        result = check_enforcement(tmp_path)
        assert result["action"] == "allow"
        assert result["response"] is None

    def test_enforce_no_active_denies(self, tmp_path):
        _setup_project(tmp_path, enforcement="enforce")
        result = check_enforcement(tmp_path)
        assert result["action"] == "deny"
        assert result["response"] is not None
        hook_output = result["response"]["hookSpecificOutput"]
        assert hook_output["permissionDecision"] == "deny"
        assert "No active process" in hook_output["permissionDecisionReason"]

    def test_enforce_with_active_allows(self, tmp_path):
        engine = _setup_project(tmp_path, enforcement="enforce")
        engine.start("simple", {"task_name": "test"})

        result = check_enforcement(tmp_path)
        assert result["action"] == "allow"
        assert result["response"] is None
        assert "simple" in result["reason"]

    def test_monitor_no_active_warns(self, tmp_path):
        _setup_project(tmp_path, enforcement="monitor")
        result = check_enforcement(tmp_path)
        assert result["action"] == "warn"
        assert result["response"] is not None
        hook_output = result["response"]["hookSpecificOutput"]
        assert hook_output["permissionDecision"] == "allow"
        assert "WARNING" in hook_output["additionalContext"]

    def test_monitor_with_active_allows(self, tmp_path):
        engine = _setup_project(tmp_path, enforcement="monitor")
        engine.start("simple", {"task_name": "test"})

        result = check_enforcement(tmp_path)
        assert result["action"] == "allow"
        assert result["response"] is None

    def test_no_processes_dir_defaults_off(self, tmp_path):
        # No .processes directory at all
        result = check_enforcement(tmp_path)
        assert result["action"] == "allow"

    def test_no_registry_defaults_off(self, tmp_path):
        # .processes dir exists but no registry.yaml
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")

        result = check_enforcement(tmp_path)
        assert result["action"] == "allow"

    def test_multiple_active_processes(self, tmp_path):
        engine = _setup_project(tmp_path, enforcement="enforce")
        engine.start("simple", {"task_name": "first"})
        engine.start("simple", {"task_name": "second"})

        result = check_enforcement(tmp_path)
        assert result["action"] == "allow"
        assert "simple" in result["reason"]


class TestGenerateHookConfig:
    def test_generates_dev_config(self):
        config = generate_hook_config(turnstile_dir="/path/to/turnstile")
        assert "hooks" in config
        assert "PreToolUse" in config["hooks"]
        entries = config["hooks"]["PreToolUse"]
        assert len(entries) == 1
        assert entries[0]["matcher"] == "Edit|Write"
        assert len(entries[0]["hooks"]) == 1
        cmd = entries[0]["hooks"][0]["command"]
        assert "turnstile guard" in cmd
        assert "/path/to/turnstile" in cmd
        assert "uv --directory" in cmd

    def test_generates_uvx_config(self):
        config = generate_hook_config(repo_url="https://github.com/org/repo.git")
        cmd = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "turnstile guard" in cmd
        assert "uvx" in cmd
        assert "github.com/org/repo.git" in cmd

    def test_requires_either_dir_or_url(self):
        with pytest.raises(ValueError, match="Either"):
            generate_hook_config()


class TestInstallEnforcement:
    def test_install_creates_settings(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        result = install_enforcement(
            tmp_path, "enforce", turnstile_dir="/opt/turnstile"
        )
        assert result["installed"]
        assert result["mode"] == "enforce"

        settings_path = tmp_path / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text())
        assert "hooks" in settings
        pre_tool = settings["hooks"]["PreToolUse"]
        assert len(pre_tool) == 1
        assert "turnstile guard" in pre_tool[0]["hooks"][0]["command"]

    def test_install_preserves_existing_settings(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        # Pre-existing settings
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {"customSetting": True, "hooks": {"PostToolUse": [{"matcher": ".*"}]}}
        (claude_dir / "settings.json").write_text(json.dumps(existing))

        install_enforcement(tmp_path, "enforce", turnstile_dir="/opt/turnstile")

        settings = json.loads((claude_dir / "settings.json").read_text())
        assert settings["customSetting"] is True
        assert "PostToolUse" in settings["hooks"]
        assert "PreToolUse" in settings["hooks"]

    def test_install_replaces_old_turnstile_hook(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        # Install once
        install_enforcement(tmp_path, "enforce", turnstile_dir="/old/path")
        # Install again with different path
        install_enforcement(tmp_path, "monitor", turnstile_dir="/new/path")

        settings = json.loads(
            (tmp_path / ".claude" / "settings.json").read_text()
        )
        pre_tool = settings["hooks"]["PreToolUse"]
        # Should only have one entry, not two
        assert len(pre_tool) == 1
        assert "/new/path" in pre_tool[0]["hooks"][0]["command"]

    def test_uninstall_removes_hook(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        # Install then remove
        install_enforcement(tmp_path, "enforce", turnstile_dir="/opt/turnstile")
        result = install_enforcement(tmp_path, "off")

        assert result["installed"]
        assert result["mode"] == "off"

        settings = json.loads(
            (tmp_path / ".claude" / "settings.json").read_text()
        )
        # hooks key should be gone entirely
        assert "hooks" not in settings or "PreToolUse" not in settings.get("hooks", {})

    def test_uninstall_preserves_non_turnstile_hooks(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]},
                ],
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(existing))

        # Install turnstile hook alongside existing
        install_enforcement(tmp_path, "enforce", turnstile_dir="/opt/turnstile")

        settings = json.loads((claude_dir / "settings.json").read_text())
        assert len(settings["hooks"]["PreToolUse"]) == 2

        # Remove turnstile hook
        install_enforcement(tmp_path, "off")

        settings = json.loads((claude_dir / "settings.json").read_text())
        # Only the non-turnstile hook should remain
        assert len(settings["hooks"]["PreToolUse"]) == 1
        assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


    def test_install_uvx_mode(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        result = install_enforcement(
            tmp_path, "monitor", repo_url="https://github.com/org/repo.git"
        )
        assert result["installed"]
        assert result["mode"] == "monitor"

        settings = json.loads(
            (tmp_path / ".claude" / "settings.json").read_text()
        )
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "uvx" in cmd
        assert "github.com/org/repo.git" in cmd


class TestUpdateRegistryEnforcement:
    def test_creates_registry_if_missing(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        update_registry_enforcement(tmp_path, "enforce")

        registry_path = proc_dir / "registry.yaml"
        assert registry_path.exists()
        raw = yaml.safe_load(registry_path.read_text())
        assert raw["settings"]["enforcement"] == "enforce"

    def test_updates_existing_registry(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        registry = {
            "version": "1.0",
            "local": ["my-process"],
            "settings": {
                "enforcement": "off",
                "log_retention_days": 30,
            },
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

        update_registry_enforcement(tmp_path, "monitor")

        raw = yaml.safe_load((proc_dir / "registry.yaml").read_text())
        assert raw["settings"]["enforcement"] == "monitor"
        # Other settings preserved
        assert raw["settings"]["log_retention_days"] == 30
        assert raw["local"] == ["my-process"]

    def test_creates_processes_dir_if_missing(self, tmp_path):
        update_registry_enforcement(tmp_path, "enforce")
        assert (tmp_path / ".processes" / "registry.yaml").exists()


class TestRunGuard:
    """Test the stdin-based guard entry point."""

    def test_guard_with_enforce_no_active(self, tmp_path, monkeypatch, capsys):
        _setup_project(tmp_path, enforcement="enforce")

        hook_input = json.dumps({
            "session_id": "test-session",
            "cwd": str(tmp_path),
            "tool_name": "Write",
            "tool_input": {"file_path": "/some/file.py", "content": "x = 1"},
        })

        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(hook_input))

        from turnstile_core.guard import run_guard
        run_guard()

        captured = capsys.readouterr()
        if captured.out.strip():
            response = json.loads(captured.out)
            assert response["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_guard_with_off_no_output(self, tmp_path, monkeypatch, capsys):
        _setup_project(tmp_path, enforcement="off")

        hook_input = json.dumps({
            "session_id": "test-session",
            "cwd": str(tmp_path),
            "tool_name": "Write",
            "tool_input": {},
        })

        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(hook_input))

        from turnstile_core.guard import run_guard
        run_guard()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

    def test_guard_fails_open_on_bad_input(self, monkeypatch, capsys):
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))

        from turnstile_core.guard import run_guard
        run_guard()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

    def test_guard_fails_open_on_missing_cwd(self, monkeypatch, capsys):
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({})))

        from turnstile_core.guard import run_guard
        run_guard()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""
