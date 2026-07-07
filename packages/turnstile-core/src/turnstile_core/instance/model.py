"""Models for process instances: the runtime record of a single run.

A definition says what a process *is*; an instance says where one
particular run of it *stands* — current state, parameter bindings,
transition history, overrides, and parent/child links.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (the timestamp format
    used throughout instance records and logs)."""
    return datetime.now(timezone.utc).isoformat()


def generate_id() -> str:
    """Generate a short unique instance ID."""
    return uuid.uuid4().hex[:6]


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

    model_config = {"populate_by_name": True}
