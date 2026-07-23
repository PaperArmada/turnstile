"""Tests for turnstile_cli.claude_adapter (the Claude Code adapter).

Relocated from turnstile-core's test_guard.py when the adapter moved out
of core (GH #32). Covers hook config generation, settings.json
install/uninstall merging (including the GH #39 shared-group contract),
and the stdin-based guard entry point. The agnostic enforcement logic
these tests used to sit beside is covered in
packages/turnstile-core/tests/test_enforcement.py.
"""

import io
import json

import pytest
import yaml

from turnstile_core.engine import Engine

from turnstile_cli.claude_adapter import (
    generate_hook_config,
    install_enforcement,
    run_guard,
)

SIMPLE_PROCESS = """
name: simple
description: "A minimal process for testing"
version: "1.0.0"

parameters:
  - name: task_name
    description: "Name of the task"
    required: true

states:
  - id: start
    type: initial
    transitions: [working]

  - id: working
    description: "Do the work"
    transitions: [done]

  - id: done
    type: terminal
"""


def _setup_project(tmp_path, enforcement="off"):
    """Set up a project with a process definition and registry."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir(exist_ok=True)
    (proc_dir / "simple.yaml").write_text(SIMPLE_PROCESS)

    if enforcement != "off":
        registry = {
            "version": "1.0",
            "settings": {"enforcement": enforcement},
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

    return Engine(tmp_path)


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


CUSTOM_HOOK_COMMAND = "python /opt/hooks/lint_check.py"


def _pre_tool_use_commands(settings):
    """Flatten all PreToolUse hook commands across groups."""
    return [
        hk.get("command", "")
        for group in settings.get("hooks", {}).get("PreToolUse", [])
        for hk in group.get("hooks", [])
    ]


class TestSharedGroupHookPreservation:
    """Regression tests for GH #39: install/off merge drops non-turnstile
    hooks that share a PreToolUse group with the turnstile guard hook.

    Contract: filtering happens at the hook level within each group. Only
    hook entries whose command contains "turnstile guard" are removed;
    sibling hooks in the same group survive; a group is dropped only when
    its hooks list becomes empty; unrelated groups are untouched.
    """

    def _write_shared_group_settings(self, tmp_path):
        """Settings where a custom hook shares a group with turnstile guard."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    "uv --directory /old/turnstile "
                                    "run --package turnstile-cli "
                                    "turnstile guard"
                                ),
                            },
                            {"type": "command", "command": CUSTOM_HOOK_COMMAND},
                        ],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        return claude_dir / "settings.json"

    @pytest.mark.parametrize("mode", ["monitor", "enforce"])
    def test_install_preserves_custom_hook_sharing_group_with_turnstile(
        self, tmp_path, mode
    ):
        """Reinstalling over a shared group keeps the sibling custom hook
        and replaces the turnstile entry rather than dropping the group."""
        (tmp_path / ".processes").mkdir()
        settings_path = self._write_shared_group_settings(tmp_path)

        install_enforcement(tmp_path, mode, turnstile_dir="/new/turnstile")

        settings = json.loads(settings_path.read_text())
        commands = _pre_tool_use_commands(settings)
        assert CUSTOM_HOOK_COMMAND in commands, (
            "custom hook sharing a group with turnstile guard was lost"
        )
        turnstile_cmds = [c for c in commands if "turnstile guard" in c]
        assert len(turnstile_cmds) == 1
        assert "/new/turnstile" in turnstile_cmds[0]

    def test_off_preserves_custom_hook_sharing_group_with_turnstile(
        self, tmp_path
    ):
        """Turning enforcement off removes only the turnstile hook entry;
        the sibling custom hook stays in its group with its matcher."""
        (tmp_path / ".processes").mkdir()
        settings_path = self._write_shared_group_settings(tmp_path)

        install_enforcement(tmp_path, "off")

        settings = json.loads(settings_path.read_text())
        commands = _pre_tool_use_commands(settings)
        assert CUSTOM_HOOK_COMMAND in commands, (
            "custom hook sharing a group with turnstile guard was lost"
        )
        assert not any("turnstile guard" in c for c in commands)
        surviving_group = settings["hooks"]["PreToolUse"][0]
        assert surviving_group["matcher"] == "Edit|Write"

    def test_off_removes_group_when_turnstile_is_only_hook(self, tmp_path):
        """Guard-rail (passes pre-fix): a group left empty after removing
        the turnstile hook is dropped, and the PreToolUse key with it,
        while unrelated hook events are untouched."""
        (tmp_path / ".processes").mkdir()
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    "uv --directory /old/turnstile "
                                    "run --package turnstile-cli "
                                    "turnstile guard"
                                ),
                            }
                        ],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {"type": "command", "command": CUSTOM_HOOK_COMMAND}
                        ],
                    }
                ],
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))

        install_enforcement(tmp_path, "off")

        settings = json.loads((claude_dir / "settings.json").read_text())
        assert "PreToolUse" not in settings.get("hooks", {})
        assert len(settings["hooks"]["PostToolUse"]) == 1

    def test_install_preserves_custom_hook_in_separate_group(self, tmp_path):
        """Guard-rail (passes pre-fix): a custom hook in its own group is
        untouched by install, which adds the turnstile group alongside it."""
        (tmp_path / ".processes").mkdir()
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": CUSTOM_HOOK_COMMAND}
                        ],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))

        install_enforcement(tmp_path, "monitor", turnstile_dir="/new/turnstile")

        settings = json.loads((claude_dir / "settings.json").read_text())
        commands = _pre_tool_use_commands(settings)
        assert CUSTOM_HOOK_COMMAND in commands
        turnstile_cmds = [c for c in commands if "turnstile guard" in c]
        assert len(turnstile_cmds) == 1
        bash_groups = [
            g for g in settings["hooks"]["PreToolUse"] if g["matcher"] == "Bash"
        ]
        assert len(bash_groups) == 1
        assert bash_groups[0]["hooks"] == [
            {"type": "command", "command": CUSTOM_HOOK_COMMAND}
        ]

    def test_install_passes_through_group_without_hooks_key(self, tmp_path):
        """A PreToolUse group lacking a "hooks" key entirely is passed
        through install unchanged, with the turnstile group added beside it."""
        (tmp_path / ".processes").mkdir()
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        hookless_group = {"matcher": "Bash"}
        settings = {"hooks": {"PreToolUse": [hookless_group]}}
        (claude_dir / "settings.json").write_text(json.dumps(settings))

        install_enforcement(tmp_path, "monitor", turnstile_dir="/new/turnstile")

        settings = json.loads((claude_dir / "settings.json").read_text())
        pre_tool = settings["hooks"]["PreToolUse"]
        assert hookless_group in pre_tool
        turnstile_cmds = [
            c for c in _pre_tool_use_commands(settings) if "turnstile guard" in c
        ]
        assert len(turnstile_cmds) == 1

    def test_install_passes_through_group_with_empty_hooks_list(self, tmp_path):
        """A PreToolUse group whose "hooks" list is empty is passed through
        install unchanged, not mistaken for an emptied turnstile group."""
        (tmp_path / ".processes").mkdir()
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        empty_group = {"matcher": "Bash", "hooks": []}
        settings = {"hooks": {"PreToolUse": [empty_group]}}
        (claude_dir / "settings.json").write_text(json.dumps(settings))

        install_enforcement(tmp_path, "monitor", turnstile_dir="/new/turnstile")

        settings = json.loads((claude_dir / "settings.json").read_text())
        pre_tool = settings["hooks"]["PreToolUse"]
        assert empty_group in pre_tool
        turnstile_cmds = [
            c for c in _pre_tool_use_commands(settings) if "turnstile guard" in c
        ]
        assert len(turnstile_cmds) == 1

    def test_reinstall_over_shared_group_is_idempotent(self, tmp_path):
        """Running install twice with identical arguments over a shared
        group yields byte-identical settings.json with one turnstile entry."""
        (tmp_path / ".processes").mkdir()
        settings_path = self._write_shared_group_settings(tmp_path)

        install_enforcement(tmp_path, "monitor", turnstile_dir="/new/turnstile")
        after_first = settings_path.read_bytes()

        install_enforcement(tmp_path, "monitor", turnstile_dir="/new/turnstile")
        after_second = settings_path.read_bytes()

        assert after_second == after_first
        settings = json.loads(after_second)
        commands = _pre_tool_use_commands(settings)
        assert CUSTOM_HOOK_COMMAND in commands
        turnstile_cmds = [c for c in commands if "turnstile guard" in c]
        assert len(turnstile_cmds) == 1


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

        monkeypatch.setattr("sys.stdin", io.StringIO(hook_input))
        run_guard()

        captured = capsys.readouterr()
        assert captured.out.strip(), (
            "enforce mode with no active instance must emit a deny response; "
            "silence means run_guard failed open"
        )
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

        monkeypatch.setattr("sys.stdin", io.StringIO(hook_input))
        run_guard()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

    def test_guard_fails_open_on_bad_input(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        run_guard()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

    def test_guard_fails_open_on_missing_cwd(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({})))
        run_guard()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

    def test_guard_with_active_surfaces_context(self, tmp_path, monkeypatch, capsys):
        """Guard should include state context when a process is active."""
        engine = _setup_project(tmp_path, enforcement="monitor")
        engine.start("simple", {"task_name": "test"})

        hook_input = json.dumps({
            "session_id": "test-session",
            "cwd": str(tmp_path),
            "tool_name": "Edit",
            "tool_input": {},
        })

        monkeypatch.setattr("sys.stdin", io.StringIO(hook_input))
        run_guard()

        captured = capsys.readouterr()
        assert captured.out.strip(), (
            "monitor mode with an active instance must emit an allow-plus-"
            "context response; silence means run_guard failed open"
        )
        response = json.loads(captured.out)
        hook_output = response["hookSpecificOutput"]
        assert hook_output["permissionDecision"] == "allow"
        assert "simple" in hook_output["additionalContext"]
        assert "start" in hook_output["additionalContext"]

    def test_guard_with_monitor_no_active_warns(self, tmp_path, monkeypatch, capsys):
        """Monitor mode with no active process allows the edit but warns."""
        _setup_project(tmp_path, enforcement="monitor")

        hook_input = json.dumps({
            "session_id": "test-session",
            "cwd": str(tmp_path),
            "tool_name": "Write",
            "tool_input": {"file_path": "/some/file.py", "content": "x = 1"},
        })

        monkeypatch.setattr("sys.stdin", io.StringIO(hook_input))
        run_guard()

        captured = capsys.readouterr()
        assert captured.out.strip(), (
            "monitor mode with no active instance must emit an allow-plus-"
            "warning response; silence means run_guard failed open"
        )
        response = json.loads(captured.out)
        hook_output = response["hookSpecificOutput"]
        assert hook_output["permissionDecision"] == "allow"
        assert hook_output["additionalContext"].startswith("WARNING:")
