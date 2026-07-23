"""Pins for two crash classes closed in commit 653bc09.

- ``StateStore.append_log`` is fail-open: log.txt is the human
  convenience view, so an unwritable log (read-only file, full disk)
  must never abort the state operation it narrates. Pre-fix, the log
  write could raise AFTER the instance JSON had persisted, reporting
  failure for work that landed. The failing-action path is pinned in
  test_action_failures.py; these cover the other call sites (create,
  plain transition, complete, abandon).
- A torn or schema-invalid instance state file raises
  ``CorruptInstanceError`` naming the exact file, instead of a bare
  JSONDecodeError/ValidationError, across ``load``, ``load_any``,
  ``list_completed``, and ``list_abandoned``. The tolerant
  ``list_active_with_errors`` path (enforcement fail-closed, F5) is
  unchanged and pinned in test_enforcement.py::TestCorruptStateFile.
"""

import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest

from turnstile_core.engine import Engine
from turnstile_core.errors import CorruptInstanceError
from turnstile_core.persistence import StateStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def project(tmp_path) -> Path:
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
    return tmp_path


@pytest.fixture()
def store(tmp_path) -> StateStore:
    return StateStore(tmp_path / ".process-state")


@contextmanager
def _read_only(path: Path):
    """Make ``path`` unwritable for the duration (restored for cleanup)."""
    path.touch()
    path.chmod(0o444)
    try:
        yield
    finally:
        path.chmod(0o644)


class TestFailOpenLog:
    """A read-only log.txt must not abort any state operation."""

    def test_read_only_log_does_not_abort_instance_creation(
        self, store: StateStore
    ):
        with _read_only(store.log_path):
            instance = store.create("proc", "start")

        # The state change landed and is loadable; the STARTED line was
        # dropped silently (also proves the write really failed, rather
        # than the chmod having no effect).
        loaded = store.load(instance.instance_id)
        assert loaded.current_state == "start"
        assert store.log_path.read_text() == ""

    async def test_read_only_log_does_not_abort_a_plain_transition(
        self, project: Path
    ):
        engine = Engine(project)
        iid = engine.start("simple", {"task_name": "t"})["instance_id"]

        log_path = project / ".process-state" / "log.txt"
        with _read_only(log_path):
            result = await engine.transition(iid, "working")

        assert result.success is True
        assert result.new_state == "working"
        # The persisted instance reflects the transition.
        store = StateStore(project / ".process-state")
        assert store.load(iid).current_state == "working"

    def test_read_only_log_does_not_abort_terminal_completion(
        self, store: StateStore
    ):
        instance = store.create("proc", "start")

        with _read_only(store.log_path):
            store.complete(instance)

        # The file moved out of active/ into completed/ despite the
        # unwritable log; the COMPLETED line was dropped silently.
        assert store.list_active() == []
        loaded = store.load_any(instance.instance_id)
        assert loaded.status == "completed"
        assert "COMPLETED" not in store.log_path.read_text()

    def test_read_only_log_does_not_abort_abandon(self, store: StateStore):
        instance = store.create("proc", "start")

        with _read_only(store.log_path):
            store.abandon(instance, "changed priorities")

        assert store.list_active() == []
        loaded = store.load_any(instance.instance_id)
        assert loaded.status == "abandoned"
        assert "ABANDONED" not in store.log_path.read_text()


class TestCorruptInstanceFiles:
    """An unparseable state file names itself instead of raising bare."""

    def _torn_active_file(self, store: StateStore) -> Path:
        path = store.active_dir / "proc-torn12345678.json"
        path.write_text('{"instance_id": "torn12')
        return path

    def test_torn_active_file_names_the_file_on_load(self, store: StateStore):
        path = self._torn_active_file(store)
        with pytest.raises(CorruptInstanceError) as excinfo:
            store.load("torn12345678")
        assert str(path) in str(excinfo.value)

    def test_torn_archived_file_names_the_file_on_load_any(
        self, store: StateStore
    ):
        instance = store.create("proc", "start")
        store.complete(instance)
        archived = next(store.completed_dir.rglob("*.json"))
        archived.write_text('{"instance_id": "abc12')

        with pytest.raises(CorruptInstanceError) as excinfo:
            store.load_any(instance.instance_id)
        assert str(archived) in str(excinfo.value)

    def test_schema_invalid_but_valid_json_is_reported_corrupt(
        self, store: StateStore
    ):
        # Parses as JSON but fails model validation: current_state missing.
        path = store.active_dir / "proc-bad12345678.json"
        path.write_text(
            '{"instance_id": "bad12345678", "process_name": "proc", '
            '"started_at": "2026-01-01T00:00:00+00:00", '
            '"updated_at": "2026-01-01T00:00:00+00:00"}'
        )
        with pytest.raises(CorruptInstanceError) as excinfo:
            store.load("bad12345678")
        assert str(path) in str(excinfo.value)

    def test_list_completed_names_the_torn_archive_file(
        self, store: StateStore
    ):
        instance = store.create("proc", "start")
        store.complete(instance)
        archived = next(store.completed_dir.rglob("*.json"))
        archived.write_text("not json at all")

        with pytest.raises(CorruptInstanceError) as excinfo:
            store.list_completed()
        assert str(archived) in str(excinfo.value)

    def test_list_abandoned_names_the_torn_archive_file(
        self, store: StateStore
    ):
        instance = store.create("proc", "start")
        store.abandon(instance, "testing")
        archived = next(store.abandoned_dir.rglob("*.json"))
        archived.write_text("not json at all")

        with pytest.raises(CorruptInstanceError) as excinfo:
            store.list_abandoned()
        assert str(archived) in str(excinfo.value)
