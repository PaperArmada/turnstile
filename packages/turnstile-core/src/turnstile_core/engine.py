"""State machine engine: the central orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from turnstile_core.admin import (
    check_migration,
    diff_definitions,
    generate_mermaid,
    simulate_dry_run,
)
from turnstile_core.errors import (
    DefinitionError,
    InstanceNotFoundError,
    ProcessNotFoundError,
    TransitionError,
)
from turnstile_core.loader import discover_definitions, load_definition, load_registry
from turnstile_core.models import ProcessDefinition, StateType
from turnstile_core.persistence import (
    HistoryEntry,
    OverrideEntry,
    ProcessInstance,
    StateStore,
    _now_iso,
)
from turnstile_core.validator import (
    ValidationResult,
    has_blocking_failures,
    run_validations,
    substitute_params,
    run_command,
)


@dataclass
class TransitionResult:
    """Result of a state transition attempt."""

    success: bool
    new_state: str
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    available_transitions: list[str] = field(default_factory=list)
    message: str = ""


def _vr_to_dict(vr: ValidationResult) -> dict[str, Any]:
    """Convert a ValidationResult to a serializable dict."""
    return {
        "command": vr.command,
        "expect": vr.expect,
        "passed": vr.passed,
        "output": vr.output,
        "exit_code": vr.exit_code,
        "message": vr.message,
        "severity": vr.severity.value,
        "error": vr.error,
        "elapsed_ms": vr.elapsed_ms,
    }


class Engine:
    """The turnstile process engine.

    Composes the loader, validator, and persistence layer into a
    single interface for managing process instances.
    """

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.registry = load_registry(project_root)
        self._definitions: dict[str, tuple[ProcessDefinition, str]] = {}
        self._store = StateStore(
            project_root / self.registry.settings.state_dir
        )
        self._load_definitions()

    def _load_definitions(self) -> None:
        self._definitions = discover_definitions(self.project_root)

    def reload(self) -> None:
        """Reload definitions from disk (e.g., after editing YAML)."""
        self.registry = load_registry(self.project_root)
        self._load_definitions()

    def _get_definition(self, name: str) -> tuple[ProcessDefinition, str]:
        if name not in self._definitions:
            raise ProcessNotFoundError(f"No process definition named '{name}'")
        return self._definitions[name]

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def list_processes(self) -> list[dict[str, Any]]:
        """List all available process definitions."""
        result = []
        for name, (defn, _hash) in self._definitions.items():
            result.append({
                "name": defn.name,
                "description": defn.description,
                "version": defn.version,
                "source": "local",
            })
        return result

    def start(
        self,
        name: str,
        parameters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Start a new process instance."""
        defn, def_hash = self._get_definition(name)
        params = parameters or {}

        # Validate required parameters
        for p in defn.parameters:
            if p.required and p.name not in params:
                raise DefinitionError(
                    f"Required parameter '{p.name}' not provided"
                )
            if p.name not in params and p.default is not None:
                params[p.name] = p.default

        initial = defn.initial_state()
        instance = self._store.create(
            process_name=defn.name,
            initial_state=initial.id,
            version=defn.version,
            definition_hash=def_hash,
            parameters=params,
        )

        return {
            "instance_id": instance.instance_id,
            "process_name": instance.process_name,
            "current_state": instance.current_state,
            "available_transitions": initial.transitions,
            "parameters": instance.parameters,
        }

    def status(
        self, instance_id: str | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Get status of active process instance(s)."""
        if instance_id:
            instance = self._store.load(instance_id)
            defn, _ = self._get_definition(instance.process_name)
            state = defn.get_state(instance.current_state)
            return {
                "instance_id": instance.instance_id,
                "process_name": instance.process_name,
                "current_state": instance.current_state,
                "available_transitions": state.transitions if state else [],
                "started_at": instance.started_at,
                "updated_at": instance.updated_at,
                "parameters": instance.parameters,
                "history_length": len(instance.history),
            }

        instances = self._store.list_active()
        result = []
        for inst in instances:
            defn_entry = self._definitions.get(inst.process_name)
            transitions = []
            if defn_entry:
                state = defn_entry[0].get_state(inst.current_state)
                transitions = state.transitions if state else []
            result.append({
                "instance_id": inst.instance_id,
                "process_name": inst.process_name,
                "current_state": inst.current_state,
                "available_transitions": transitions,
                "started_at": inst.started_at,
                "updated_at": inst.updated_at,
            })
        return result

    async def transition(
        self, instance_id: str, target_state: str
    ) -> TransitionResult:
        """Attempt a legal transition to a new state."""
        instance = self._store.load(instance_id)
        defn, _ = self._get_definition(instance.process_name)

        current = defn.get_state(instance.current_state)
        if current is None:
            raise TransitionError(
                f"Current state '{instance.current_state}' not found in definition"
            )

        # Check transition is legal
        if target_state not in current.transitions:
            raise TransitionError(
                f"Transition from '{current.id}' to '{target_state}' is not allowed. "
                f"Legal transitions: {current.transitions}"
            )

        target = defn.get_state(target_state)
        if target is None:
            raise TransitionError(
                f"Target state '{target_state}' not found in definition"
            )

        all_results: list[ValidationResult] = []

        # Run on_exit validations for current state
        if current.on_exit and current.on_exit.validations:
            exit_results = await run_validations(
                current.on_exit.validations,
                instance.parameters,
                self.project_root,
            )
            all_results.extend(exit_results)

            if has_blocking_failures(exit_results):
                return TransitionResult(
                    success=False,
                    new_state=instance.current_state,
                    validation_results=[_vr_to_dict(r) for r in all_results],
                    available_transitions=current.transitions,
                    message="on_exit validation failed",
                )

        # Run on_enter validations for target state
        if target.on_enter and target.on_enter.validations:
            enter_results = await run_validations(
                target.on_enter.validations,
                instance.parameters,
                self.project_root,
            )
            all_results.extend(enter_results)

            if has_blocking_failures(enter_results):
                return TransitionResult(
                    success=False,
                    new_state=instance.current_state,
                    validation_results=[_vr_to_dict(r) for r in all_results],
                    available_transitions=current.transitions,
                    message="on_enter validation failed",
                )

        # Transition succeeds
        history_entry = HistoryEntry(**{
            "from": instance.current_state,
            "to": target_state,
            "at": _now_iso(),
            "validations": [_vr_to_dict(r) for r in all_results],
        })
        instance.current_state = target_state
        instance.history.append(history_entry)

        # Run on_enter actions
        if target.on_enter and target.on_enter.actions:
            for action in target.on_enter.actions:
                cmd = substitute_params(action.command, instance.parameters)
                try:
                    await run_command(cmd, self.project_root, timeout=60)
                except Exception:
                    pass  # Actions are best-effort

        # Handle terminal states
        if target.type == StateType.terminal:
            self._store.complete(instance)
        else:
            self._store.save(instance)

        self._store.append_log(
            f"TRANSITION {instance.process_name}-{instance.instance_id}: "
            f"{history_entry.from_state} -> {target_state}"
        )

        return TransitionResult(
            success=True,
            new_state=target_state,
            validation_results=[_vr_to_dict(r) for r in all_results],
            available_transitions=target.transitions,
        )

    async def skip(
        self, instance_id: str, target_state: str, reason: str
    ) -> TransitionResult:
        """Force-skip to a state, bypassing transition rules."""
        if self.registry.settings.require_override_reason and not reason:
            raise TransitionError("Override reason is required")

        instance = self._store.load(instance_id)
        defn, _ = self._get_definition(instance.process_name)

        target = defn.get_state(target_state)
        if target is None:
            raise TransitionError(
                f"Target state '{target_state}' not found in definition"
            )

        override = OverrideEntry(
            from_state=instance.current_state,
            to_state=target_state,
            at=_now_iso(),
            reason=reason,
        )

        history_entry = HistoryEntry(**{
            "from": instance.current_state,
            "to": target_state,
            "at": _now_iso(),
            "validations": [],
        })

        instance.current_state = target_state
        instance.history.append(history_entry)
        instance.overrides.append(override)

        if target.type == StateType.terminal:
            self._store.complete(instance)
        else:
            self._store.save(instance)

        self._store.append_log(
            f"SKIP {instance.process_name}-{instance.instance_id}: "
            f"{override.from_state} -> {target_state} ({reason})"
        )

        return TransitionResult(
            success=True,
            new_state=target_state,
            validation_results=[],
            available_transitions=target.transitions,
            message=f"Override logged: {reason}",
        )

    def abandon(self, instance_id: str, reason: str) -> dict[str, Any]:
        """Abandon a process instance."""
        instance = self._store.load(instance_id)
        final_state = instance.current_state
        self._store.abandon(instance, reason)
        return {
            "success": True,
            "final_state": final_state,
            "reason": reason,
        }

    def undo(self, instance_id: str, reason: str) -> TransitionResult:
        """Revert the last transition (administrative correction).

        Cannot undo past a skip or another undo.
        """
        instance = self._store.load(instance_id)
        defn, _ = self._get_definition(instance.process_name)

        if not instance.history:
            raise TransitionError("No transitions to undo")

        last = instance.history[-1]

        # Check if the last transition was a skip
        if instance.overrides and instance.overrides[-1].to_state == last.to_state:
            raise TransitionError("Cannot undo a skip; use process_skip to move forward")

        previous_state = last.from_state
        prev = defn.get_state(previous_state)
        if prev is None:
            raise TransitionError(
                f"Previous state '{previous_state}' not found in definition"
            )

        instance.current_state = previous_state
        instance.history.pop()
        self._store.save(instance)

        self._store.append_log(
            f"UNDO {instance.process_name}-{instance.instance_id}: "
            f"{last.to_state} -> {previous_state} ({reason})"
        )

        return TransitionResult(
            success=True,
            new_state=previous_state,
            available_transitions=prev.transitions,
            message=f"Undone: {reason}",
        )

    def handoff(
        self, instance_id: str, to_user: str, reason: str
    ) -> dict[str, Any]:
        """Log an ownership transfer (metadata only)."""
        instance = self._store.load(instance_id)
        previous_owner = instance.started_by
        instance.started_by = to_user
        self._store.save(instance)

        self._store.append_log(
            f"HANDOFF {instance.process_name}-{instance.instance_id}: "
            f"{previous_owner or '(none)'} -> {to_user} ({reason})"
        )

        return {
            "success": True,
            "previous_owner": previous_owner,
            "new_owner": to_user,
            "reason": reason,
        }

    def history(self, instance_id: str) -> list[dict[str, Any]]:
        """Get full transition history for a process instance.

        Searches active, completed, and abandoned instances.
        """
        instance = self._store.load_any(instance_id)
        return [
            {
                "from_state": h.from_state,
                "to_state": h.to_state,
                "timestamp": h.at,
                "validations": h.validations,
                "triggered_by": h.triggered_by,
            }
            for h in instance.history
        ]

    def graph(self, name: str) -> dict[str, Any]:
        """Generate a Mermaid state diagram for a process definition."""
        defn, _ = self._get_definition(name)
        return generate_mermaid(defn)

    def dry_run(self, name: str, path: list[str] | None = None) -> list[dict[str, Any]]:
        """Simulate a process execution without persistence.

        If path is provided, walks that specific sequence of states.
        Otherwise, walks all states showing their configuration.
        """
        defn, _ = self._get_definition(name)
        return simulate_dry_run(defn, path)

    def diff(self, path_a: str, path_b: str) -> dict[str, Any]:
        """Diff two process definition files."""
        defn_a = load_definition(Path(path_a))
        defn_b = load_definition(Path(path_b))
        return diff_definitions(defn_a, defn_b)

    def migrate(self, instance_id: str) -> dict[str, Any]:
        """Check if an in-flight instance needs migration.

        Compares the instance's definition_hash against the current
        definition file and reports compatibility.
        """
        instance = self._store.load(instance_id)
        defn, current_hash = self._get_definition(instance.process_name)
        return check_migration(
            instance.current_state,
            instance.definition_hash,
            defn,
            current_hash,
        )

    def validate_definition(self, path: str) -> dict[str, Any]:
        """Validate a process definition file."""
        try:
            defn = load_definition(Path(path))
            return {
                "valid": True,
                "name": defn.name,
                "version": defn.version,
                "states": [s.id for s in defn.states],
                "errors": [],
                "warnings": [],
            }
        except Exception as e:
            return {
                "valid": False,
                "errors": [str(e)],
                "warnings": [],
            }
