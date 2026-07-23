"""Tests for turnstile_core.persistence."""

import json
import os
import stat
from pathlib import Path

import pytest

from turnstile_core.errors import InstanceNotFoundError
from turnstile_core.persistence import HistoryEntry, ProcessInstance, StateStore


def _make_instance(
    store: StateStore,
    process_name: str,
    instance_id: str,
    current_state: str = "start",
) -> ProcessInstance:
    """Persist an instance with a hand-chosen ID via the public API."""
    now = "2026-01-01T00:00:00+00:00"
    instance = ProcessInstance(
        instance_id=instance_id,
        process_name=process_name,
        current_state=current_state,
        started_at=now,
        updated_at=now,
    )
    store.save(instance)
    return instance


def _raise_oserror(*args, **kwargs):
    raise OSError("simulated disk failure")


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


class TestAtomicSave:
    @pytest.fixture()
    def store(self, tmp_path) -> StateStore:
        return StateStore(tmp_path / ".process-state")

    def test_failed_save_preserves_previous_instance_on_disk(
        self, store: StateStore, monkeypatch
    ):
        instance = store.create("test", "start")
        path = store.active_dir / f"test-{instance.instance_id}.json"
        assert path.exists()

        instance.current_state = "step2"

        def torn_write_text(self, text, *args, **kwargs):
            # Simulate a crash partway through an in-place write: half the
            # payload lands on disk, then the write fails.
            with open(self, "w") as f:
                f.write(text[: len(text) // 2])
            raise OSError("simulated disk failure")

        monkeypatch.setattr(Path, "write_text", torn_write_text)
        monkeypatch.setattr(os, "replace", _raise_oserror)

        with pytest.raises(OSError):
            store.save(instance)

        # The on-disk file must still parse as the previous good instance.
        data = json.loads(path.read_text())
        assert data["instance_id"] == instance.instance_id
        assert data["current_state"] == "start"

    def test_failed_save_leaves_no_temp_or_stray_files(
        self, store: StateStore, monkeypatch
    ):
        instance = store.create("test", "start")
        instance.current_state = "step2"

        monkeypatch.setattr(os, "replace", _raise_oserror)

        with pytest.raises(OSError):
            store.save(instance)

        entries = sorted(p.name for p in store.active_dir.iterdir())
        assert entries == [f"test-{instance.instance_id}.json"]

    def test_failed_write_before_rename_preserves_state_and_cleans_temp(
        self, store: StateStore, monkeypatch
    ):
        # Failure during the write phase (before the rename boundary), unlike
        # the tests above which inject failure at os.replace itself.
        instance = store.create("test", "start")
        path = store.active_dir / f"test-{instance.instance_id}.json"

        instance.current_state = "step2"
        monkeypatch.setattr(os, "fsync", _raise_oserror)

        with pytest.raises(OSError):
            store.save(instance)

        data = json.loads(path.read_text())
        assert data["current_state"] == "start"
        entries = sorted(p.name for p in store.active_dir.iterdir())
        assert entries == [f"test-{instance.instance_id}.json"]

    def test_state_file_mode_honors_umask(self, store: StateStore):
        prior_umask = os.umask(0o022)
        try:
            instance = store.create("test", "start")
            instance.current_state = "step2"
            store.save(instance)
        finally:
            os.umask(prior_umask)

        path = store.active_dir / f"test-{instance.instance_id}.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o644


class TestExactIdResolution:
    @pytest.fixture()
    def store(self, tmp_path) -> StateStore:
        return StateStore(tmp_path / ".process-state")

    def test_load_rejects_id_that_is_tail_of_longer_id(self, store: StateStore):
        _make_instance(store, "beta", "xabc123")
        with pytest.raises(InstanceNotFoundError, match="No instance with ID"):
            store.load("abc123")

    def test_load_resolves_exact_id_among_superstring_ids(
        self, store: StateStore
    ):
        _make_instance(store, "alpha", "abc123")
        _make_instance(store, "beta", "xabc123")
        assert store.load("abc123").process_name == "alpha"
        assert store.load("xabc123").process_name == "beta"

    def test_load_rejects_id_that_only_appears_in_process_name(
        self, store: StateStore
    ):
        _make_instance(store, "abc123-flow", "deadbeef4321")
        with pytest.raises(InstanceNotFoundError, match="No instance with ID"):
            store.load("abc123")

    def test_load_rejects_id_matching_middle_filename_component(
        self, store: StateStore
    ):
        # Stem is "flow-abc123-deadbeef4321": "-abc123" appears with a
        # delimiter on both sides but is not the final component.
        _make_instance(store, "flow-abc123", "deadbeef4321")
        with pytest.raises(InstanceNotFoundError, match="No instance with ID"):
            store.load("abc123")

    def test_load_does_not_mistake_longer_completed_id_for_requested_one(
        self, store: StateStore
    ):
        decoy = _make_instance(store, "beta", "xabc123")
        store.complete(decoy)
        with pytest.raises(InstanceNotFoundError, match="No instance with ID"):
            store.load("abc123")

    def test_load_any_rejects_id_that_is_tail_of_longer_active_id(
        self, store: StateStore
    ):
        _make_instance(store, "beta", "xabc123")
        with pytest.raises(InstanceNotFoundError, match="checked active"):
            store.load_any("abc123")

    def test_load_any_resolves_exact_id_among_completed_instances(
        self, store: StateStore
    ):
        wanted = _make_instance(store, "alpha", "abc123")
        store.complete(wanted)
        decoy = _make_instance(store, "beta", "xabc123")
        store.complete(decoy)

        loaded = store.load_any("abc123")
        assert loaded.process_name == "alpha"
        assert loaded.status == "completed"

    def test_load_empty_id_raises_even_when_instances_exist(
        self, store: StateStore
    ):
        _make_instance(store, "alpha", "abc123def456")
        with pytest.raises(InstanceNotFoundError, match="No instance with ID"):
            store.load("")

    def test_load_any_rejects_id_only_in_completed_process_name(
        self, store: StateStore
    ):
        decoy = _make_instance(store, "abc123-flow", "deadbeef4321")
        store.complete(decoy)
        with pytest.raises(InstanceNotFoundError, match="checked active"):
            store.load_any("abc123")


class TestIdGeneration:
    @pytest.fixture()
    def store(self, tmp_path) -> StateStore:
        return StateStore(tmp_path / ".process-state")

    def test_new_ids_are_twelve_lowercase_hex_chars(self, store: StateStore):
        instance = store.create("test", "start")
        assert len(instance.instance_id) == 12
        assert set(instance.instance_id) <= set("0123456789abcdef")

    def test_legacy_six_char_id_still_loads(self, store: StateStore):
        _make_instance(store, "legacy", "a1b2c3")
        loaded = store.load("a1b2c3")
        assert loaded.instance_id == "a1b2c3"
        assert loaded.process_name == "legacy"


class TestTempFileHygiene:
    @pytest.fixture()
    def store(self, tmp_path) -> StateStore:
        return StateStore(tmp_path / ".process-state")

    def test_temp_files_are_invisible_to_listing_and_loading(
        self, store: StateStore
    ):
        real = store.create("test", "start")
        tmp_file = store.active_dir / ".proc-fake999.json.ab12.tmp"
        tmp_file.write_text("{ not even json")

        instances, corrupt = store.list_active_with_errors()
        assert [i.instance_id for i in instances] == [real.instance_id]
        assert corrupt == []

        with pytest.raises(InstanceNotFoundError, match="No instance with ID"):
            store.load("fake999")
