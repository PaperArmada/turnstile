"""Tests for turnstile_core.persistence."""

import json

import pytest

from turnstile_core.errors import InstanceNotFoundError
from turnstile_core.persistence import HistoryEntry, ProcessInstance, StateStore


class TestStateStore:
    @pytest.fixture()
    def store(self, tmp_path) -> StateStore:
        return StateStore(tmp_path / ".process-state")

    def test_create(self, store: StateStore):
        instance = store.create(
            process_name="test-process",
            initial_state="start",
            version="1.0.0",
            parameters={"key": "value"},
        )
        assert instance.instance_id
        assert instance.process_name == "test-process"
        assert instance.current_state == "start"
        assert instance.parameters == {"key": "value"}
        assert instance.status == "active"

    def test_load(self, store: StateStore):
        created = store.create("test", "start")
        loaded = store.load(created.instance_id)
        assert loaded.instance_id == created.instance_id
        assert loaded.process_name == "test"
        assert loaded.current_state == "start"

    def test_load_missing_raises(self, store: StateStore):
        with pytest.raises(InstanceNotFoundError, match="No instance with ID"):
            store.load("nonexistent")

    def test_load_completed_gives_specific_error(self, store: StateStore):
        instance = store.create("test", "start")
        iid = instance.instance_id
        store.complete(instance)
        with pytest.raises(InstanceNotFoundError, match="completed.*not active"):
            store.load(iid)

    def test_load_abandoned_gives_specific_error(self, store: StateStore):
        instance = store.create("test", "start")
        iid = instance.instance_id
        store.abandon(instance, "testing")
        with pytest.raises(InstanceNotFoundError, match="abandoned.*not active"):
            store.load(iid)

    def test_save_updates(self, store: StateStore):
        instance = store.create("test", "start")
        instance.current_state = "step2"
        instance.history.append(
            HistoryEntry(**{
                "from": "start",
                "to": "step2",
                "at": "2026-01-01T00:00:00Z",
                "validations": [],
            })
        )
        store.save(instance)

        loaded = store.load(instance.instance_id)
        assert loaded.current_state == "step2"
        assert len(loaded.history) == 1

    def test_list_active(self, store: StateStore):
        store.create("proc-a", "start")
        store.create("proc-b", "start")
        active = store.list_active()
        assert len(active) == 2
        names = {i.process_name for i in active}
        assert names == {"proc-a", "proc-b"}

    def test_complete(self, store: StateStore):
        instance = store.create("test", "start")
        iid = instance.instance_id
        store.complete(instance)

        # No longer in active
        assert store.list_active() == []

        # Should be in completed/
        completed_files = list(store.completed_dir.rglob("*.json"))
        assert len(completed_files) == 1
        data = json.loads(completed_files[0].read_text())
        assert data["instance_id"] == iid
        assert data["status"] == "completed"

    def test_abandon(self, store: StateStore):
        instance = store.create("test", "start")
        iid = instance.instance_id
        store.abandon(instance, "changed priorities")

        assert store.list_active() == []

        abandoned_files = list(store.abandoned_dir.rglob("*.json"))
        assert len(abandoned_files) == 1
        data = json.loads(abandoned_files[0].read_text())
        assert data["instance_id"] == iid
        assert data["status"] == "abandoned"

    def test_log_written(self, store: StateStore):
        store.create("test", "start")
        assert store.log_path.exists()
        log_content = store.log_path.read_text()
        assert "STARTED" in log_content

    def test_directory_structure(self, store: StateStore):
        assert store.active_dir.exists()
        assert store.completed_dir.exists()
        assert store.abandoned_dir.exists()

    def test_load_any_active(self, store: StateStore):
        instance = store.create("test", "start")
        loaded = store.load_any(instance.instance_id)
        assert loaded.instance_id == instance.instance_id
        assert loaded.status == "active"

    def test_load_any_completed(self, store: StateStore):
        instance = store.create("test", "start")
        iid = instance.instance_id
        store.complete(instance)

        loaded = store.load_any(iid)
        assert loaded.instance_id == iid
        assert loaded.status == "completed"

    def test_load_any_abandoned(self, store: StateStore):
        instance = store.create("test", "start")
        iid = instance.instance_id
        store.abandon(instance, "test reason")

        loaded = store.load_any(iid)
        assert loaded.instance_id == iid
        assert loaded.status == "abandoned"

    def test_load_any_missing_raises(self, store: StateStore):
        with pytest.raises(InstanceNotFoundError, match="checked active"):
            store.load_any("nonexistent")
