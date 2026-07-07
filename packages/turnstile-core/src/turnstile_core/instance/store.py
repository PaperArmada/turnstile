"""File-based state persistence for process instances."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from turnstile_core.errors import InstanceNotFoundError
from turnstile_core.instance.model import (
    ProcessInstance,
    generate_id,
    now_iso,
)


class StateStore:
    """File-based state persistence for process instances."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.active_dir = state_dir / "active"
        self.completed_dir = state_dir / "completed"
        self.abandoned_dir = state_dir / "abandoned"
        self.log_path = state_dir / "log.txt"
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
        instance_id = generate_id()
        now = now_iso()
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
            if path.suffix == ".json" and instance_id in path.stem:
                data = json.loads(path.read_text())
                return ProcessInstance(**data)

        # Check archived directories for a better error message
        for label, archive_dir in (
            ("completed", self.completed_dir),
            ("abandoned", self.abandoned_dir),
        ):
            for path in archive_dir.rglob("*.json"):
                if instance_id in path.stem:
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
            if path.suffix == ".json" and instance_id in path.stem:
                data = json.loads(path.read_text())
                return ProcessInstance(**data)

        # Search archived directories (organized by month)
        for archive_dir in (self.completed_dir, self.abandoned_dir):
            for path in archive_dir.rglob("*.json"):
                if instance_id in path.stem:
                    data = json.loads(path.read_text())
                    return ProcessInstance(**data)

        raise InstanceNotFoundError(
            f"No instance with ID '{instance_id}' (checked active, "
            f"completed, and abandoned)"
        )

    def save(self, instance: ProcessInstance) -> None:
        """Persist an instance back to disk."""
        instance.updated_at = now_iso()
        self._write(instance)

    def list_active(self) -> list[ProcessInstance]:
        """List all active process instances."""
        instances = []
        for path in sorted(self.active_dir.glob("*.json")):
            data = json.loads(path.read_text())
            instances.append(ProcessInstance(**data))
        return instances

    def complete(self, instance: ProcessInstance) -> None:
        """Move an instance to completed/."""
        instance.status = "completed"
        instance.updated_at = now_iso()

        month_dir = self.completed_dir / datetime.now(timezone.utc).strftime("%Y-%m")
        month_dir.mkdir(exist_ok=True)

        src = self._active_path(instance.process_name, instance.instance_id)
        dst = month_dir / src.name

        # Write updated data to destination, then remove source
        dst.write_text(
            instance.model_dump_json(indent=2, by_alias=True)
        )
        if src.exists():
            src.unlink()

        self.append_log(
            f"COMPLETED {instance.process_name}-{instance.instance_id}"
        )

    def abandon(self, instance: ProcessInstance, reason: str) -> None:
        """Move an instance to abandoned/."""
        instance.status = "abandoned"
        instance.updated_at = now_iso()

        month_dir = self.abandoned_dir / datetime.now(timezone.utc).strftime("%Y-%m")
        month_dir.mkdir(exist_ok=True)

        src = self._active_path(instance.process_name, instance.instance_id)
        dst = month_dir / src.name

        dst.write_text(
            instance.model_dump_json(indent=2, by_alias=True)
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
        timestamp = now_iso()
        with open(self.log_path, "a") as f:
            f.write(f"[{timestamp}] {event}\n")

    def _write(self, instance: ProcessInstance) -> None:
        path = self._active_path(instance.process_name, instance.instance_id)
        path.write_text(instance.model_dump_json(indent=2, by_alias=True))
