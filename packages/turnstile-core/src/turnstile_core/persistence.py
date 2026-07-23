"""File-based state persistence for process instances."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from turnstile_core.errors import InstanceNotFoundError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Instance models
# ---------------------------------------------------------------------------


class HistoryEntry(BaseModel):
    """A single transition in a process instance's history."""

    from_state: str = Field(alias="from")
    to_state: str = Field(alias="to")
    at: str
    triggered_by: str = ""
    role: str = ""
    session_id: str = ""
    validations: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class OverrideEntry(BaseModel):
    """A record of a skip/override."""

    from_state: str
    to_state: str
    at: str
    reason: str
    triggered_by: str = ""


class ProcessInstance(BaseModel):
    """Runtime state of a process instance, persisted as JSON."""

    instance_id: str
    process_name: str
    process_version: str = ""
    definition_hash: str = ""
    parameters: dict[str, str] = Field(default_factory=dict)
    current_state: str
    started_at: str
    updated_at: str
    started_by: str = ""
    history: list[HistoryEntry] = Field(default_factory=list)
    overrides: list[OverrideEntry] = Field(default_factory=list)
    status: str = "active"  # active, completed, abandoned

    # Subprocess tracking
    parent_instance_id: str | None = None
    parent_state_id: str | None = None
    child_instance_id: str | None = None
    suspended: bool = False

    # Wait state tracking
    waiting: bool = False
    signal_data: dict[str, Any] | None = None

    # Next sequence number for the instance's event stream (events.jsonl).
    # Persisted with the instance so the shadow stream stays ordered across
    # sessions; defaults to 0 for state files that predate the stream.
    event_seq: int = 0

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# State store
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_id() -> str:
    return uuid.uuid4().hex[:12]


def _matches_id(path: Path, instance_id: str) -> bool:
    """True if ``path`` is the state file for ``instance_id``.

    Filenames are ``{process_name}-{instance_id}.json``, so require the ID as
    the exact final ``-``-delimited component. A substring test would let an
    ID that happens to appear inside a process name or a longer ID resolve to
    the wrong instance.

    Strictly this is "preceded by a dash": a queried ID containing ``-`` can
    match across the name/ID boundary (e.g. querying ``abc-123`` matches
    process ``proc-abc`` id ``123``). Generated IDs are pure hex, so this is
    unreachable through ``create()``; it also means a full ``name-id`` token
    pasted from a log resolves, which is acceptable.
    """
    return path.suffix == ".json" and path.stem.endswith(f"-{instance_id}")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via a same-directory temp file.

    ``os.replace`` is atomic on POSIX and Windows, so readers see either the
    old content or the new content, never a truncated file. The temp file uses
    a ``.tmp`` suffix so it can never match the ``*.json`` globs that
    enumerate instances.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates the file 0600; restore the umask-derived mode a
        # plain write would have produced, so permissions don't depend on
        # which code path last wrote the file.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp_name, 0o666 & ~umask)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class StateStore:
    """File-based state persistence for process instances."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.active_dir = state_dir / "active"
        self.completed_dir = state_dir / "completed"
        self.abandoned_dir = state_dir / "abandoned"
        self.log_path = state_dir / "log.txt"
        self.events_path = state_dir / "events.jsonl"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.active_dir.mkdir(parents=True, exist_ok=True)
        self.completed_dir.mkdir(parents=True, exist_ok=True)
        self.abandoned_dir.mkdir(parents=True, exist_ok=True)

    def _instance_filename(self, process_name: str, instance_id: str) -> str:
        return f"{process_name}-{instance_id}.json"

    def _active_path(self, process_name: str, instance_id: str) -> Path:
        return self.active_dir / self._instance_filename(process_name, instance_id)

    def create(
        self,
        process_name: str,
        initial_state: str,
        version: str = "",
        definition_hash: str = "",
        parameters: dict[str, str] | None = None,
        started_by: str = "",
    ) -> ProcessInstance:
        """Create a new process instance and persist it."""
        instance_id = _generate_id()
        now = _now_iso()
        instance = ProcessInstance(
            instance_id=instance_id,
            process_name=process_name,
            process_version=version,
            definition_hash=definition_hash,
            parameters=parameters or {},
            current_state=initial_state,
            started_at=now,
            updated_at=now,
            started_by=started_by,
        )
        self._write(instance)
        self.append_log(
            f"STARTED {process_name}-{instance_id} at {initial_state}"
        )
        return instance

    def load(self, instance_id: str) -> ProcessInstance:
        """Load an active instance by ID. Raises InstanceNotFoundError."""
        for path in self.active_dir.iterdir():
            if _matches_id(path, instance_id):
                data = json.loads(path.read_text())
                return ProcessInstance(**data)

        # Check archived directories for a better error message
        for label, archive_dir in (
            ("completed", self.completed_dir),
            ("abandoned", self.abandoned_dir),
        ):
            for path in archive_dir.rglob("*.json"):
                if _matches_id(path, instance_id):
                    raise InstanceNotFoundError(
                        f"Instance '{instance_id}' is {label} (not active)"
                    )

        raise InstanceNotFoundError(f"No instance with ID '{instance_id}'")

    def load_any(self, instance_id: str) -> ProcessInstance:
        """Load an instance from any directory (active, completed, abandoned).

        Searches active first, then completed, then abandoned.
        Raises InstanceNotFoundError if not found anywhere.
        """
        # Try active first
        for path in self.active_dir.iterdir():
            if _matches_id(path, instance_id):
                data = json.loads(path.read_text())
                return ProcessInstance(**data)

        # Search archived directories (organized by month)
        for archive_dir in (self.completed_dir, self.abandoned_dir):
            for path in archive_dir.rglob("*.json"):
                if _matches_id(path, instance_id):
                    data = json.loads(path.read_text())
                    return ProcessInstance(**data)

        raise InstanceNotFoundError(
            f"No instance with ID '{instance_id}' (checked active, "
            f"completed, and abandoned)"
        )

    def save(self, instance: ProcessInstance) -> None:
        """Persist an instance back to disk."""
        instance.updated_at = _now_iso()
        self._write(instance)

    def list_active_with_errors(
        self,
    ) -> tuple[list[ProcessInstance], list[Path]]:
        """List active instances, tolerating unreadable files.

        Returns ``(instances, corrupt_paths)``. A file that cannot be parsed
        as a valid instance is skipped and its path recorded, never raised, so
        one torn JSON does not blind the whole listing. Callers that make
        security decisions (see enforcement.check_enforcement) must treat a
        non-empty corrupt list as "state unknown", not "no restrictions"
        (SECURITY-NOTES F5).
        """
        instances: list[ProcessInstance] = []
        corrupt: list[Path] = []
        for path in sorted(self.active_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                instances.append(ProcessInstance(**data))
            except Exception:
                logger.warning(
                    "Skipping unreadable process state file: %s", path
                )
                corrupt.append(path)
        return instances, corrupt

    def list_active(self) -> list[ProcessInstance]:
        """List all active process instances, skipping unreadable ones."""
        instances, _ = self.list_active_with_errors()
        return instances

    def complete(self, instance: ProcessInstance) -> None:
        """Move an instance to completed/."""
        instance.status = "completed"
        instance.updated_at = _now_iso()

        month_dir = self.completed_dir / datetime.now(timezone.utc).strftime("%Y-%m")
        month_dir.mkdir(exist_ok=True)

        src = self._active_path(instance.process_name, instance.instance_id)
        dst = month_dir / src.name

        # Write updated data to destination, then remove source
        _atomic_write_text(
            dst, instance.model_dump_json(indent=2, by_alias=True)
        )
        if src.exists():
            src.unlink()

        self.append_log(
            f"COMPLETED {instance.process_name}-{instance.instance_id}"
        )

    def abandon(self, instance: ProcessInstance, reason: str) -> None:
        """Move an instance to abandoned/."""
        instance.status = "abandoned"
        instance.updated_at = _now_iso()

        month_dir = self.abandoned_dir / datetime.now(timezone.utc).strftime("%Y-%m")
        month_dir.mkdir(exist_ok=True)

        src = self._active_path(instance.process_name, instance.instance_id)
        dst = month_dir / src.name

        _atomic_write_text(
            dst, instance.model_dump_json(indent=2, by_alias=True)
        )
        if src.exists():
            src.unlink()

        self.append_log(
            f"ABANDONED {instance.process_name}-{instance.instance_id}: {reason}"
        )

    def list_completed(self) -> list[ProcessInstance]:
        """List all completed process instances."""
        instances = []
        for path in self.completed_dir.rglob("*.json"):
            data = json.loads(path.read_text())
            instances.append(ProcessInstance(**data))
        return instances

    def list_abandoned(self) -> list[ProcessInstance]:
        """List all abandoned process instances."""
        instances = []
        for path in self.abandoned_dir.rglob("*.json"):
            data = json.loads(path.read_text())
            instances.append(ProcessInstance(**data))
        return instances

    def append_log(self, event: str) -> None:
        """Append an event to the log file."""
        timestamp = _now_iso()
        with open(self.log_path, "a") as f:
            f.write(f"[{timestamp}] {event}\n")

    def append_event(self, event: dict[str, Any]) -> None:
        """Append one typed event to the append-only JSONL stream.

        The stream (events.jsonl) is a shadow record: instance JSON remains
        the source of truth, and events are appended in parallel so the
        stream's shape can be validated ahead of the event-sourced flip.
        One compact JSON object per line; the file is only ever appended,
        never rewritten. Non-JSON-native payload values are stringified
        (default=str) rather than dropping the event: a silently missing
        event would make the stream diverge from history undetectably.

        Unlike instance writes there is no fsync here; losing tail events
        to a crash is acceptable for a shadow stream, torn instance JSON is
        not. See docs/reference.md ("Event stream") for the consumer
        contract.
        """
        line = json.dumps(event, separators=(",", ":"), default=str) + "\n"
        with open(self.events_path, "a") as f:
            f.write(line)

    def _write(self, instance: ProcessInstance) -> None:
        path = self._active_path(instance.process_name, instance.instance_id)
        _atomic_write_text(
            path, instance.model_dump_json(indent=2, by_alias=True)
        )
