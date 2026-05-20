"""State machine engine: the central orchestrator."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_UNRESOLVED_TEMPLATE_RE = re.compile(r"\$\{([^}]+)\}")


def _check_resolved(
    child_process: str,
    child_key: str,
    value: str,
    available: dict[str, str],
) -> None:
    """Raise SubprocessError if value still contains ${...} placeholders.

    Called after substituting parameter_map templates. A surviving
    placeholder means the template referenced a variable that was not in
    the parent's parameters and was not supplied via transition metadata.
    Silently passing the literal "${var}" as a child parameter masks the
    error until far downstream; failing fast at dispatch time is the
    correct behavior.
    """
    unresolved = _UNRESOLVED_TEMPLATE_RE.findall(value)
    if not unresolved:
        return
    raise SubprocessError(
        f"Dispatch to '{child_process}' has unresolved parameter(s) in "
        f"parameter_map entry '{child_key}': "
        f"{', '.join(sorted(set(unresolved)))}. "
        f"Available keys: {sorted(available.keys())}. "
        f"Pass the missing value(s) via the transition metadata argument."
    )

from turnstile_core.analytics import compute_analytics
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
    SubprocessError,
    TransitionError,
)
from turnstile_core.loader import (
    DiscoveredDefinition,
    definition_hash,
    discover_definitions,
    discover_definitions_full,
    load_definition,
    load_registry,
)
from turnstile_core.models import ProcessDefinition, StateType
from turnstile_core.persistence import (
    HistoryEntry,
    OverrideEntry,
    ProcessInstance,
    StateStore,
    _now_iso,
)
from turnstile_core.notifications import fire_notification
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
    # Role declared on the target state
    role: str = ""
    # Agent context for the target state (provisioning)
    agent_context: dict[str, Any] | None = None
    # Subprocess delegation info
    subprocess_started: str | None = None  # child instance_id if subprocess started
    parent_resumed: bool = False
    parent_instance_id: str | None = None
    parent_available_transitions: list[str] = field(default_factory=list)
    # Skill directives for the target state
    skill_directives: list[dict[str, str]] = field(default_factory=list)
    # Required metadata to transition out of the new state
    required_metadata: list[dict[str, str]] = field(default_factory=list)
    # Summary on terminal state completion
    summary: dict[str, Any] | None = None


def _compute_summary(instance: ProcessInstance) -> dict[str, Any]:
    """Compute a summary of a completed process instance."""
    states_visited = []
    for h in instance.history:
        if not states_visited or states_visited[-1] != h.from_state:
            states_visited.append(h.from_state)
        states_visited.append(h.to_state)

    # Deduplicate while preserving order for the unique set
    unique_states = list(dict.fromkeys(states_visited))

    # Count validations
    total_validations = 0
    passed_validations = 0
    failed_validations = 0
    for h in instance.history:
        for v in h.validations:
            total_validations += 1
            if v.get("passed"):
                passed_validations += 1
            else:
                failed_validations += 1

    # Elapsed time
    try:
        started = datetime.fromisoformat(instance.started_at)
        ended = datetime.fromisoformat(instance.updated_at)
        elapsed = ended - started
        elapsed_str = str(elapsed).split(".")[0]  # drop microseconds
    except (ValueError, TypeError):
        elapsed_str = "unknown"

    return {
        "states_visited": unique_states,
        "transition_count": len(instance.history),
        "override_count": len(instance.overrides),
        "validations_run": total_validations,
        "validations_passed": passed_validations,
        "validations_failed": failed_validations,
        "elapsed": elapsed_str,
    }


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
        self._discovered: dict[str, DiscoveredDefinition] = {}
        self._store = StateStore(
            project_root / self.registry.settings.state_dir
        )
        self._load_definitions()

    def _load_definitions(self) -> None:
        self._discovered = discover_definitions_full(self.project_root)
        self._definitions = {
            name: (d.definition, d.file_hash)
            for name, d in self._discovered.items()
        }

    def reload(self) -> None:
        """Reload definitions from disk (e.g., after editing YAML)."""
        self.registry = load_registry(self.project_root)
        self._load_definitions()

    def _get_definition(self, name: str) -> tuple[ProcessDefinition, str]:
        if name not in self._definitions:
            raise ProcessNotFoundError(f"No process definition named '{name}'")

        # Auto-reload if the file on disk has changed since we cached it
        disc = self._discovered.get(name)
        if disc and disc.source_path:
            source = Path(disc.source_path)
            if source.exists():
                current_hash = definition_hash(source)
                if current_hash != disc.file_hash:
                    try:
                        defn = load_definition(source)
                        disc.file_hash = current_hash
                        disc.definition = defn
                        self._definitions[name] = (defn, current_hash)
                    except Exception:
                        pass  # Fail open: use cached version if reload fails

        return self._definitions[name]

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def list_processes(self) -> list[dict[str, Any]]:
        """List all available process definitions."""
        result = []
        for name, disc in self._discovered.items():
            result.append({
                "name": disc.definition.name,
                "description": disc.definition.description,
                "version": disc.definition.version,
                "source": disc.source,
            })
        return result

    def info(self, name: str) -> dict[str, Any]:
        """Get detailed information about a process definition.

        Returns parameters (with descriptions/defaults), states (with
        descriptions, permissions, transitions), and metadata. Designed
        for parameter discovery before calling start().
        """
        defn, def_hash = self._get_definition(name)

        parameters = []
        for p in defn.parameters:
            param_info: dict[str, Any] = {
                "name": p.name,
                "description": p.description,
                "required": p.required,
            }
            if p.default is not None:
                param_info["default"] = p.default
            parameters.append(param_info)

        states = []
        for s in defn.states:
            state_info: dict[str, Any] = {
                "id": s.id,
                "type": s.type.value,
            }
            if s.description:
                state_info["description"] = s.description
            if s.transitions:
                state_info["transitions"] = s.transitions
            perms: dict[str, Any] = {}
            if not s.permissions.edit:
                perms["edit"] = False
            if s.permissions.edit_paths:
                perms["edit_paths"] = s.permissions.edit_paths
            if perms:
                state_info["permissions"] = perms
            if s.required_metadata:
                state_info["required_metadata"] = [
                    {"key": rm.key, "description": rm.description}
                    for rm in s.required_metadata
                ]
            states.append(state_info)

        result: dict[str, Any] = {
            "name": defn.name,
            "description": defn.description,
            "version": defn.version,
            "parameters": parameters,
            "states": states,
        }

        if defn.metadata:
            result["metadata"] = defn.metadata

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
            info: dict[str, Any] = {
                "instance_id": instance.instance_id,
                "process_name": instance.process_name,
                "current_state": instance.current_state,
                "available_transitions": (
                    [] if instance.suspended or instance.waiting
                    else (state.transitions if state else [])
                ),
                "started_at": instance.started_at,
                "updated_at": instance.updated_at,
                "parameters": instance.parameters,
                "history_length": len(instance.history),
                "suspended": instance.suspended,
            }
            if state and state.required_metadata:
                info["required_metadata"] = [
                    {"key": rm.key, "description": rm.description}
                    for rm in state.required_metadata
                ]
            if instance.suspended:
                info["child_instance_id"] = instance.child_instance_id
            if instance.waiting:
                info["waiting"] = True
                if state and state.signal:
                    info["waiting_for_signal"] = state.signal.name
            if instance.parent_instance_id:
                info["parent_instance_id"] = instance.parent_instance_id
            return info

        instances = self._store.list_active()
        result = []
        for inst in instances:
            defn_entry = self._definitions.get(inst.process_name)
            transitions = []
            if defn_entry:
                state = defn_entry[0].get_state(inst.current_state)
                transitions = state.transitions if state else []
            entry: dict[str, Any] = {
                "instance_id": inst.instance_id,
                "process_name": inst.process_name,
                "current_state": inst.current_state,
                "available_transitions": transitions,
                "started_at": inst.started_at,
                "updated_at": inst.updated_at,
                "suspended": inst.suspended,
            }
            if inst.suspended:
                entry["child_instance_id"] = inst.child_instance_id
            if inst.parent_instance_id:
                entry["parent_instance_id"] = inst.parent_instance_id
            result.append(entry)
        return result

    async def transition(
        self, instance_id: str, target_state: str,
        metadata: dict[str, Any] | None = None,
        session_id: str = "",
    ) -> TransitionResult:
        """Attempt a legal transition to a new state."""
        instance = self._store.load(instance_id)

        # Block transitions on suspended instances
        if instance.suspended:
            raise TransitionError(
                f"Instance is suspended waiting for subprocess "
                f"'{instance.child_instance_id}'. Complete or abandon "
                f"the child process first."
            )

        # Block transitions on waiting instances (use receive_signal instead)
        if instance.waiting:
            raise TransitionError(
                f"Instance is waiting for a signal. Use receive_signal() "
                f"to deliver the signal and advance the state."
            )

        defn, _ = self._get_definition(instance.process_name)

        current = defn.get_state(instance.current_state)
        if current is None:
            raise TransitionError(
                f"Current state '{instance.current_state}' not found in definition"
            )

        # Check transition is legal
        # For subprocess states, check routing targets instead of transitions
        if current.type == StateType.subprocess and current.subprocess_routing:
            all_targets = (
                current.subprocess_routing.on_complete_targets()
                + current.subprocess_routing.on_fail
            )
            if target_state not in all_targets:
                raise TransitionError(
                    f"Transition from subprocess state '{current.id}' to "
                    f"'{target_state}' is not allowed. "
                    f"Legal targets: {all_targets}"
                )
        elif target_state not in current.transitions:
            raise TransitionError(
                f"Transition from '{current.id}' to '{target_state}' is not allowed. "
                f"Legal transitions: {current.transitions}"
            )

        target = defn.get_state(target_state)
        if target is None:
            raise TransitionError(
                f"Target state '{target_state}' not found in definition"
            )

        # Check required metadata for current state
        if current.required_metadata:
            provided = metadata or {}
            missing = [
                rm.key for rm in current.required_metadata
                if rm.key not in provided
            ]
            if missing:
                descriptions = {
                    rm.key: rm.description
                    for rm in current.required_metadata
                    if rm.key in missing
                }
                detail = ", ".join(
                    f"'{k}' ({descriptions[k]})" if descriptions[k] else f"'{k}'"
                    for k in missing
                )
                raise TransitionError(
                    f"State '{current.id}' requires metadata: {detail}. "
                    f"Pass metadata={{...}} with the transition."
                )

        all_results: list[ValidationResult] = []

        # Build validation context: declared params + instance metadata
        validation_params = {
            **instance.parameters,
            "instance_id": instance.instance_id,
        }

        # Run on_exit validations for current state
        if current.on_exit and current.on_exit.validations:
            exit_results = await run_validations(
                current.on_exit.validations,
                validation_params,
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

        # Run on_exit actions for current state
        if current.on_exit and current.on_exit.actions:
            for action in current.on_exit.actions:
                cmd = substitute_params(action.command, validation_params)
                try:
                    await run_command(cmd, self.project_root, timeout=60)
                except Exception:
                    pass  # Actions are best-effort

        # Run on_enter validations for target state
        if target.on_enter and target.on_enter.validations:
            enter_results = await run_validations(
                target.on_enter.validations,
                validation_params,
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
            "role": target.role,
            "session_id": session_id,
            "validations": [_vr_to_dict(r) for r in all_results],
            "metadata": metadata or {},
        })
        instance.current_state = target_state
        instance.history.append(history_entry)

        # Run on_enter actions
        if target.on_enter and target.on_enter.actions:
            for action in target.on_enter.actions:
                cmd = substitute_params(action.command, validation_params)
                try:
                    await run_command(cmd, self.project_root, timeout=60)
                except Exception:
                    pass  # Actions are best-effort

        # Handle dispatch states (async subprocess: create child, don't suspend)
        if target.type == StateType.dispatch:
            # Extract string values from metadata for parameter forwarding
            extra_params = {
                k: str(v) for k, v in (metadata or {}).items()
                if isinstance(v, (str, int, float, bool))
            }
            child_result = self._dispatch_child(instance, target, extra_params=extra_params)
            immediate_state = defn.get_state(target.immediate)
            if immediate_state is None:
                raise TransitionError(
                    f"Dispatch immediate target '{target.immediate}' "
                    f"not found in definition"
                )

            # Second history entry for the automatic transition
            auto_entry = HistoryEntry(**{
                "from": target_state,
                "to": target.immediate,
                "at": _now_iso(),
                "triggered_by": f"dispatch: {target.process}",
                "role": immediate_state.role,
                "session_id": session_id,
                "metadata": {
                    "dispatched_instance": child_result["instance_id"],
                    "dispatched_process": child_result["process_name"],
                },
            })
            instance.current_state = target.immediate
            instance.history.append(auto_entry)
            self._store.save(instance)

            self._store.append_log(
                f"DISPATCH {instance.process_name}-{instance.instance_id}: "
                f"created {child_result['process_name']}-"
                f"{child_result['instance_id']}, "
                f"continued to {target.immediate}"
            )

            return TransitionResult(
                success=True,
                new_state=target.immediate,
                validation_results=[_vr_to_dict(r) for r in all_results],
                available_transitions=immediate_state.transitions,
                role=immediate_state.role,
                agent_context=immediate_state.agent_context.model_dump() if immediate_state.agent_context else None,
                message=(
                    f"Dispatched '{target.process}' as instance "
                    f"{child_result['instance_id']}, "
                    f"continued to '{target.immediate}'"
                ),
                subprocess_started=child_result["instance_id"],
            )

        # Handle wait states
        if target.type == StateType.wait:
            instance.waiting = True
            self._store.save(instance)

            self._store.append_log(
                f"TRANSITION {instance.process_name}-{instance.instance_id}: "
                f"{history_entry.from_state} -> {target_state} (waiting for signal "
                f"'{target.signal.name}')"
            )

            return TransitionResult(
                success=True,
                new_state=target_state,
                validation_results=[_vr_to_dict(r) for r in all_results],
                available_transitions=[],
                role=target.role,
                agent_context=target.agent_context.model_dump() if target.agent_context else None,
                message=f"Waiting for signal '{target.signal.name}'",
            )

        # Handle subprocess states
        if target.type == StateType.subprocess:
            child_result = self._start_subprocess(instance, target)
            self._store.save(instance)

            self._store.append_log(
                f"TRANSITION {instance.process_name}-{instance.instance_id}: "
                f"{history_entry.from_state} -> {target_state} "
                f"(subprocess {child_result['instance_id']} started)"
            )

            return TransitionResult(
                success=True,
                new_state=target_state,
                validation_results=[_vr_to_dict(r) for r in all_results],
                available_transitions=[],
                message=f"Subprocess '{target.process}' started",
                subprocess_started=child_result["instance_id"],
            )

        # Handle terminal states
        if target.type == StateType.terminal:
            self._store.complete(instance)
            # Check if this child completing should resume a parent
            parent_info = self._resume_parent(
                instance, "completed", child_terminal_state=target_state
            )
            # Fire on_complete notification
            await self._notify("on_complete", {
                "name": instance.process_name,
                "instance_id": instance.instance_id,
                "state": target_state,
            })
        else:
            self._store.save(instance)
            parent_info = None

        self._store.append_log(
            f"TRANSITION {instance.process_name}-{instance.instance_id}: "
            f"{history_entry.from_state} -> {target_state}"
        )

        result = TransitionResult(
            success=True,
            new_state=target_state,
            validation_results=[_vr_to_dict(r) for r in all_results],
            available_transitions=target.transitions,
            role=target.role,
            agent_context=target.agent_context.model_dump() if target.agent_context else None,
            skill_directives=[
                {"skill": sd.skill, "args": sd.args}
                for sd in target.skill_directives
            ],
            required_metadata=[
                {"key": rm.key, "description": rm.description}
                for rm in target.required_metadata
            ],
        )

        if target.type == StateType.terminal:
            result.summary = _compute_summary(instance)

        if parent_info:
            result.parent_resumed = True
            result.parent_instance_id = parent_info["parent_instance_id"]
            result.parent_available_transitions = parent_info["available_transitions"]

        return result

    def _start_subprocess(
        self, parent: ProcessInstance, state: ProcessState
    ) -> dict[str, Any]:
        """Start a child process for a subprocess state."""
        # Build child parameters from parameter_map
        child_params: dict[str, str] = {}
        if state.parameter_map:
            for child_key, template in state.parameter_map.items():
                substituted = substitute_params(template, parent.parameters)
                _check_resolved(
                    state.process, child_key, substituted, parent.parameters
                )
                child_params[child_key] = substituted

        # Start the child process
        child_defn, child_hash = self._get_definition(state.process)
        for p in child_defn.parameters:
            if p.required and p.name not in child_params:
                raise SubprocessError(
                    f"Subprocess '{state.process}' requires parameter "
                    f"'{p.name}' but it is not in parameter_map"
                )
            if p.name not in child_params and p.default is not None:
                child_params[p.name] = p.default

        initial = child_defn.initial_state()
        child_instance = self._store.create(
            process_name=child_defn.name,
            initial_state=initial.id,
            version=child_defn.version,
            definition_hash=child_hash,
            parameters=child_params,
        )

        # Link parent and child
        child_instance.parent_instance_id = parent.instance_id
        child_instance.parent_state_id = state.id
        self._store.save(child_instance)

        parent.child_instance_id = child_instance.instance_id
        parent.suspended = True

        self._store.append_log(
            f"SUBPROCESS {parent.process_name}-{parent.instance_id}: "
            f"started {child_defn.name}-{child_instance.instance_id} "
            f"at state '{state.id}'"
        )

        return {
            "instance_id": child_instance.instance_id,
            "process_name": child_instance.process_name,
            "current_state": child_instance.current_state,
            "available_transitions": initial.transitions,
        }

    def _dispatch_child(
        self, parent: ProcessInstance, state: ProcessState,
        extra_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Start a child process without suspending the parent (async dispatch).

        extra_params: additional key-value pairs (typically from transition
        metadata) merged with parent parameters for parameter_map resolution.
        Extra params take precedence over parent params.
        """
        # Merge parent parameters with extra params from transition metadata
        param_context = dict(parent.parameters)
        if extra_params:
            param_context.update(extra_params)

        child_params: dict[str, str] = {}
        if state.parameter_map:
            for child_key, template in state.parameter_map.items():
                substituted = substitute_params(template, param_context)
                _check_resolved(state.process, child_key, substituted, param_context)
                child_params[child_key] = substituted

        child_defn, child_hash = self._get_definition(state.process)
        for p in child_defn.parameters:
            if p.required and p.name not in child_params:
                raise SubprocessError(
                    f"Dispatch process '{state.process}' requires parameter "
                    f"'{p.name}' but it is not in parameter_map"
                )
            if p.name not in child_params and p.default is not None:
                child_params[p.name] = p.default

        initial = child_defn.initial_state()
        child_instance = self._store.create(
            process_name=child_defn.name,
            initial_state=initial.id,
            version=child_defn.version,
            definition_hash=child_hash,
            parameters=child_params,
        )

        # Link child to parent (but don't suspend parent)
        child_instance.parent_instance_id = parent.instance_id
        child_instance.parent_state_id = state.id
        if state.assign_to:
            child_instance.started_by = state.assign_to
        self._store.save(child_instance)

        self._store.append_log(
            f"DISPATCH {parent.process_name}-{parent.instance_id}: "
            f"created {child_defn.name}-{child_instance.instance_id}"
        )

        return {
            "instance_id": child_instance.instance_id,
            "process_name": child_instance.process_name,
            "current_state": child_instance.current_state,
            "available_transitions": initial.transitions,
        }

    def _resume_parent(
        self,
        child: ProcessInstance,
        outcome: str,
        child_terminal_state: str | None = None,
    ) -> dict[str, Any] | None:
        """Resume a parent after subprocess completion or abandonment.

        Returns parent info dict if a parent was resumed, None otherwise.
        child_terminal_state is the terminal state the child ended in
        (used for dict-based on_complete routing).
        """
        if not child.parent_instance_id:
            return None

        try:
            parent = self._store.load(child.parent_instance_id)
        except InstanceNotFoundError:
            return None

        if not parent.suspended:
            return None

        parent_defn, _ = self._get_definition(parent.process_name)
        subprocess_state = parent_defn.get_state(parent.current_state)

        if subprocess_state is None or not subprocess_state.subprocess_routing:
            return None

        routing = subprocess_state.subprocess_routing
        if outcome == "completed" and child_terminal_state:
            available = routing.resolve_on_complete(child_terminal_state)
        elif outcome == "completed":
            available = routing.on_complete_targets()
        else:
            available = routing.on_fail

        parent.suspended = False
        parent.child_instance_id = None
        self._store.save(parent)

        self._store.append_log(
            f"SUBPROCESS_DONE {parent.process_name}-{parent.instance_id}: "
            f"child {child.process_name}-{child.instance_id} {outcome}, "
            f"available transitions: {available}"
        )

        return {
            "parent_instance_id": parent.instance_id,
            "available_transitions": available,
        }

    async def skip(
        self, instance_id: str, target_state: str, reason: str,
        session_id: str = "",
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
            "triggered_by": f"skip: {reason}",
            "role": target.role,
            "session_id": session_id,
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

        # Fire on_override notification
        await self._notify("on_override", {
            "name": instance.process_name,
            "instance_id": instance.instance_id,
            "step": f"{override.from_state} -> {target_state}",
            "reason": reason,
            "user": instance.started_by or "unknown",
        })

        return TransitionResult(
            success=True,
            new_state=target_state,
            validation_results=[],
            available_transitions=target.transitions,
            role=target.role,
            message=f"Override logged: {reason}",
        )

    def receive_signal(
        self, instance_id: str, signal_name: str,
        data: dict[str, Any],
        target_state: str | None = None,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Deliver a signal to a waiting process instance.

        Validates the signal name and required fields, stores the signal
        data, and optionally transitions to a target state.

        Args:
            instance_id: The ID of the waiting process instance.
            signal_name: Must match the wait state's signal spec name.
            data: Signal payload (key-value pairs).
            target_state: Optional target to transition to immediately.
            session_id: Session identifier for audit trail.
        """
        instance = self._store.load(instance_id)

        if not instance.waiting:
            raise TransitionError(
                f"Instance '{instance_id}' is not waiting for a signal"
            )

        defn, _ = self._get_definition(instance.process_name)
        current = defn.get_state(instance.current_state)
        if current is None or current.type != StateType.wait:
            raise TransitionError(
                f"Current state '{instance.current_state}' is not a wait state"
            )

        if current.signal.name != signal_name:
            raise TransitionError(
                f"Expected signal '{current.signal.name}', "
                f"got '{signal_name}'"
            )

        # Validate required fields
        missing = [
            f.key for f in current.signal.required_fields
            if f.key not in data
        ]
        if missing:
            raise TransitionError(
                f"Signal missing required fields: {', '.join(missing)}"
            )

        # Store signal data and clear waiting flag
        instance.signal_data = data
        instance.waiting = False

        result: dict[str, Any] = {
            "success": True,
            "signal_received": signal_name,
            "signal_data": data,
        }

        if target_state:
            # Validate target is in the wait state's transitions
            if target_state not in current.transitions:
                raise TransitionError(
                    f"'{target_state}' is not a valid transition from "
                    f"wait state '{current.id}'. "
                    f"Available: {current.transitions}"
                )

            target = defn.get_state(target_state)
            history_entry = HistoryEntry(**{
                "from": instance.current_state,
                "to": target_state,
                "at": _now_iso(),
                "triggered_by": f"signal: {signal_name}",
                "role": target.role if target else "",
                "session_id": session_id,
                "metadata": {"signal_data": data},
            })

            instance.current_state = target_state
            instance.history.append(history_entry)

            # Handle dispatch states reached via signal
            if target and target.type == StateType.dispatch:
                extra_params = {
                    k: str(v) for k, v in data.items()
                    if isinstance(v, (str, int, float, bool))
                }
                child_result = self._dispatch_child(
                    instance, target, extra_params=extra_params,
                )
                immediate_state = defn.get_state(target.immediate)

                auto_entry = HistoryEntry(**{
                    "from": target_state,
                    "to": target.immediate,
                    "at": _now_iso(),
                    "triggered_by": f"dispatch: {target.process}",
                    "role": immediate_state.role if immediate_state else "",
                    "session_id": session_id,
                    "metadata": {
                        "dispatched_instance": child_result["instance_id"],
                        "dispatched_process": child_result["process_name"],
                    },
                })
                instance.current_state = target.immediate
                instance.history.append(auto_entry)
                self._store.save(instance)

                self._store.append_log(
                    f"SIGNAL {instance.process_name}-{instance.instance_id}: "
                    f"received '{signal_name}', dispatched {child_result['process_name']}-"
                    f"{child_result['instance_id']}, continued to {target.immediate}"
                )

                result["new_state"] = target.immediate
                result["available_transitions"] = immediate_state.transitions if immediate_state else []
                result["role"] = immediate_state.role if immediate_state else ""
                result["subprocess_started"] = child_result["instance_id"]
            elif target and target.type == StateType.terminal:
                self._store.complete(instance)

                self._store.append_log(
                    f"SIGNAL {instance.process_name}-{instance.instance_id}: "
                    f"received '{signal_name}', transitioned to {target_state}"
                )

                result["new_state"] = target_state
                result["available_transitions"] = []
                result["role"] = target.role if target else ""
            else:
                self._store.save(instance)

                self._store.append_log(
                    f"SIGNAL {instance.process_name}-{instance.instance_id}: "
                    f"received '{signal_name}', transitioned to {target_state}"
                )

                result["new_state"] = target_state
                result["available_transitions"] = target.transitions if target else []
                result["role"] = target.role if target else ""
        else:
            # Signal received but no transition yet; unlock transitions
            self._store.save(instance)

            self._store.append_log(
                f"SIGNAL {instance.process_name}-{instance.instance_id}: "
                f"received '{signal_name}', awaiting transition"
            )

            result["new_state"] = instance.current_state
            result["available_transitions"] = current.transitions

        return result

    def abandon(self, instance_id: str, reason: str) -> dict[str, Any]:
        """Abandon a process instance."""
        instance = self._store.load(instance_id)
        final_state = instance.current_state
        self._store.abandon(instance, reason)

        result: dict[str, Any] = {
            "success": True,
            "final_state": final_state,
            "reason": reason,
        }

        # If this was a child process, resume the parent
        parent_info = self._resume_parent(instance, "abandoned")
        if parent_info:
            result["parent_resumed"] = True
            result["parent_instance_id"] = parent_info["parent_instance_id"]
            result["parent_available_transitions"] = parent_info["available_transitions"]

        return result

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
        result = []
        for h in instance.history:
            entry: dict[str, Any] = {
                "from_state": h.from_state,
                "to_state": h.to_state,
                "timestamp": h.at,
                "validations": h.validations,
                "triggered_by": h.triggered_by,
            }
            if h.metadata:
                entry["metadata"] = h.metadata
            result.append(entry)
        return result

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

    def check_completed(
        self,
        process_name: str,
        state: str | None = None,
        parameters: dict[str, str] | None = None,
        include_active: bool = False,
        parent_instance_id: str | None = None,
    ) -> dict[str, Any]:
        """Check whether a matching process instance exists and reached a state.

        Used by CI checks and git hooks. Returns a dict with:
        - passed: bool
        - matches: list of matching instances (summary)
        - message: human-readable result

        Searches completed instances by default. Set include_active=True
        to also search active instances.
        """
        candidates: list[ProcessInstance] = list(self._store.list_completed())
        if include_active:
            candidates.extend(self._store.list_active())

        # Filter by process name
        matches = [c for c in candidates if c.process_name == process_name]

        # Filter by parent instance
        if parent_instance_id:
            matches = [
                m for m in matches
                if m.parent_instance_id == parent_instance_id
            ]

        # Filter by parameters
        if parameters:
            filtered = []
            for inst in matches:
                if all(
                    inst.parameters.get(k) == v
                    for k, v in parameters.items()
                ):
                    filtered.append(inst)
            matches = filtered

        # Filter by state reached (appears in history or is current_state)
        if state:
            filtered = []
            for inst in matches:
                visited = {inst.current_state}
                for h in inst.history:
                    visited.add(h.from_state)
                    visited.add(h.to_state)
                if state in visited:
                    filtered.append(inst)
            matches = filtered

        passed = len(matches) > 0
        summaries = [
            {
                "instance_id": m.instance_id,
                "current_state": m.current_state,
                "status": m.status,
                "started_at": m.started_at,
                "updated_at": m.updated_at,
            }
            for m in matches
        ]

        if passed:
            msg = f"Found {len(matches)} matching instance(s) of '{process_name}'"
            if state:
                msg += f" that reached '{state}'"
        else:
            msg = f"No matching instance of '{process_name}' found"
            if state:
                msg += f" that reached '{state}'"
            if parameters:
                param_str = ", ".join(f"{k}={v}" for k, v in parameters.items())
                msg += f" with parameters [{param_str}]"

        return {
            "passed": passed,
            "matches": summaries,
            "message": msg,
        }

    async def _notify(self, event: str, context: dict[str, str]) -> None:
        """Fire a notification if configured in registry settings."""
        notifications = self.registry.settings.notifications
        if notifications:
            await fire_notification(
                event, notifications, context, self.project_root
            )

    def analytics(self) -> dict[str, Any]:
        """Compute process analytics from archived instances."""
        return compute_analytics(self._store)

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
