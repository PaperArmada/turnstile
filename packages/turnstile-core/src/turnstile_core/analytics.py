"""Process analytics computed from archived instances."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from turnstile_core.persistence import ProcessInstance, StateStore


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp."""
    return datetime.fromisoformat(ts)


def _duration_seconds(start: str, end: str) -> float:
    """Compute duration in seconds between two ISO timestamps."""
    return (_parse_iso(end) - _parse_iso(start)).total_seconds()


def compute_analytics(store: StateStore) -> dict[str, Any]:
    """Compute analytics across all completed and abandoned instances.

    Returns:
        Dict with per-process stats: total instances, completion rate,
        avg duration, per-state avg duration, override patterns.
    """
    completed = store.list_completed()
    abandoned = store.list_abandoned()
    all_instances = completed + abandoned

    if not all_instances:
        return {"processes": {}, "total_instances": 0}

    # Group by process name
    by_process: dict[str, list[ProcessInstance]] = defaultdict(list)
    for inst in all_instances:
        by_process[inst.process_name].append(inst)

    processes: dict[str, Any] = {}
    for name, instances in sorted(by_process.items()):
        proc_completed = [i for i in instances if i.status == "completed"]
        proc_abandoned = [i for i in instances if i.status == "abandoned"]

        # Total duration (started_at -> updated_at)
        durations = []
        for inst in instances:
            try:
                dur = _duration_seconds(inst.started_at, inst.updated_at)
                durations.append(dur)
            except (ValueError, TypeError):
                pass

        # Per-state durations from history
        state_durations: dict[str, list[float]] = defaultdict(list)
        for inst in instances:
            prev_time = inst.started_at
            for h in inst.history:
                try:
                    dur = _duration_seconds(prev_time, h.at)
                    state_durations[h.from_state].append(dur)
                    prev_time = h.at
                except (ValueError, TypeError):
                    pass

        # Override patterns
        override_counts: dict[str, int] = defaultdict(int)
        total_overrides = 0
        for inst in instances:
            for ov in inst.overrides:
                key = f"{ov.from_state} -> {ov.to_state}"
                override_counts[key] += 1
                total_overrides += 1

        avg_duration = sum(durations) / len(durations) if durations else None
        state_avg = {
            state: sum(durs) / len(durs)
            for state, durs in sorted(state_durations.items())
            if durs
        }

        processes[name] = {
            "total": len(instances),
            "completed": len(proc_completed),
            "abandoned": len(proc_abandoned),
            "completion_rate": (
                len(proc_completed) / len(instances)
                if instances
                else 0.0
            ),
            "avg_duration_seconds": avg_duration,
            "state_avg_duration_seconds": state_avg,
            "total_overrides": total_overrides,
            "override_patterns": dict(
                sorted(override_counts.items(), key=lambda x: -x[1])
            ),
        }

    return {
        "processes": processes,
        "total_instances": len(all_instances),
    }
