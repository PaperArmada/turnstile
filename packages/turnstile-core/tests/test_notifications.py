"""Tests for turnstile_core.notifications."""

import shutil
from pathlib import Path

import pytest

from turnstile_core.engine import Engine
from turnstile_core.notifications import fire_notification, run_notification

FIXTURES = Path(__file__).parent / "fixtures"


class TestRunNotification:
    @pytest.mark.asyncio
    async def test_simple_command(self, tmp_path):
        result = await run_notification("echo hello", {}, tmp_path)
        assert result["success"] is True
        assert result["stdout"] == "hello"
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_template_substitution(self, tmp_path):
        result = await run_notification(
            "echo 'Process {name} done'",
            {"name": "release"},
            tmp_path,
        )
        assert result["success"] is True
        assert result["stdout"] == "Process release done"

    @pytest.mark.asyncio
    async def test_multiple_variables(self, tmp_path):
        result = await run_notification(
            "echo '{name} {state}'",
            {"name": "deploy", "state": "done"},
            tmp_path,
        )
        assert result["success"] is True
        assert "deploy done" in result["stdout"]

    @pytest.mark.asyncio
    async def test_unknown_variable_left_as_is(self, tmp_path):
        result = await run_notification(
            "echo '{name} {unknown}'",
            {"name": "deploy"},
            tmp_path,
        )
        assert result["success"] is True
        assert "{unknown}" in result["stdout"]

    @pytest.mark.asyncio
    async def test_failed_command(self, tmp_path):
        result = await run_notification("exit 1", {}, tmp_path)
        assert result["success"] is False
        assert result["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_timeout(self, tmp_path):
        result = await run_notification("sleep 10", {}, tmp_path, timeout=1)
        assert result["success"] is False
        assert "timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_writes_to_file(self, tmp_path):
        log = tmp_path / "log.txt"
        result = await run_notification(
            f"echo 'completed' >> {log}",
            {},
            tmp_path,
        )
        assert result["success"] is True
        assert log.read_text().strip() == "completed"


class TestFireNotification:
    @pytest.mark.asyncio
    async def test_fires_matching_event(self, tmp_path):
        notifications = {"on_complete": "echo '{name} done'"}
        result = await fire_notification(
            "on_complete", notifications, {"name": "release"}, tmp_path
        )
        assert result is not None
        assert result["success"] is True
        assert "release done" in result["stdout"]

    @pytest.mark.asyncio
    async def test_returns_none_for_unconfigured_event(self, tmp_path):
        notifications = {"on_complete": "echo done"}
        result = await fire_notification(
            "on_override", notifications, {}, tmp_path
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_notifications(self, tmp_path):
        result = await fire_notification("on_complete", {}, {}, tmp_path)
        assert result is None

    @pytest.mark.asyncio
    async def test_on_override_context(self, tmp_path):
        notifications = {
            "on_override": "echo 'OVERRIDE: {step} by {user}: {reason}'"
        }
        result = await fire_notification(
            "on_override",
            notifications,
            {"step": "review -> done", "user": "dev", "reason": "urgent"},
            tmp_path,
        )
        assert result is not None
        assert result["success"] is True
        assert "OVERRIDE: review -> done by dev: urgent" in result["stdout"]


class TestEngineNotificationIntegration:
    """Test that the engine fires notifications at the right times."""

    @pytest.fixture()
    def notified_engine(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")

        log_file = tmp_path / "notifications.log"
        registry = proc_dir / "registry.yaml"
        registry.write_text(
            f'version: "1.0"\n'
            f"settings:\n"
            f"  notifications:\n"
            f"    on_complete: \"echo 'completed:{{name}}' >> {log_file}\"\n"
            f"    on_override: \"echo 'override:{{step}}' >> {log_file}\"\n"
        )
        return Engine(tmp_path), log_file

    @pytest.mark.asyncio
    async def test_on_complete_fires(self, notified_engine):
        engine, log_file = notified_engine
        inst = engine.start("simple", {"task_name": "test"})
        pid = inst["instance_id"]

        await engine.transition(pid, "working")
        await engine.transition(pid, "review")
        await engine.transition(pid, "done")

        assert log_file.exists()
        content = log_file.read_text()
        assert "completed:simple" in content

    @pytest.mark.asyncio
    async def test_on_override_fires(self, notified_engine):
        engine, log_file = notified_engine
        inst = engine.start("simple", {"task_name": "test"})
        pid = inst["instance_id"]

        await engine.skip(pid, "done", "emergency")

        assert log_file.exists()
        content = log_file.read_text()
        assert "override:start -> done" in content

    @pytest.mark.asyncio
    async def test_no_notification_on_normal_transition(self, notified_engine):
        engine, log_file = notified_engine
        inst = engine.start("simple", {"task_name": "test"})
        pid = inst["instance_id"]

        await engine.transition(pid, "working")

        # on_complete should not have fired yet
        if log_file.exists():
            assert "completed" not in log_file.read_text()
