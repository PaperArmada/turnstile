"""State machine engine: the central orchestrator."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
from turnstile_core.graph import analyze as graph_analyze
from turnstile_core.models import ProcessDefinition, ProcessState, StateType
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

    def _emit(
        self,
        event_type: str,
        instance: ProcessInstance,
        payload: dict[str, Any] | None = None,
        session_id: str = "",
        actor: str = "",
    ) -> None:
        """Append a typed event to the shadow stream (events.jsonl).

        Called immediately before the persist that lands the mutation, so
        the incremented per-instance sequence number is saved with the
        instance. Instance JSON stays the source of truth; the stream is
        validated shape for the future event-sourced substrate. Emission
        never blocks the operation: a failed append is logged and swallowed
        (fail open), because the shadow stream must not break live work.

        Contract consequences of this ordering (see docs/reference.md,
        "Event stream"): if the persist fails AFTER the append, the stream
        holds an event for a mutation that never landed, and a retried
        operation re-emits at the same seq. Consumers must treat instance
        JSON as truth and dedupe on (instance_id, seq), keeping the last
        occurrence. Sequence numbers assume a single writer per instance.
        """
        event = {
            "event_type": event_type,
            "instance_id": instance.instance_id,
            "process_name": instance.process_name,
            "seq": instance.event_seq,
            "at": _now_iso(),
            "session_id": session_id,
            "actor": actor,
            "payload": payload or {},
        }
        try:
            self._store.append_event(event)
        except Exception:
            logger.warning(
                "Failed to append event %s for %s-%s to events.jsonl",
                event_type,
                instance.process_name,
                instance.instance_id,
                exc_info=True,
            )
        else:
            # Increment only on a successful append so the stream stays
            # gapless: a swallowed failure reuses the number instead of
            # leaving a hole indistinguishable from an ordering bug.
            instance.event_seq += 1

    @staticmethod
    def _entry_payload(entry: HistoryEntry) -> dict[str, Any]:
        """History-entry data carried on transition-shaped events.

        Contains everything fold(events) needs to rebuild the entry.
        """
        return {
            "from": entry.from_state,
            "to": entry.to_state,
            "role": entry.role,
            "triggered_by": entry.triggered_by,
            "validations": entry.validations,
            "metadata": entry.metadata,
        }

    def start(
        self,
        name: str,
        parameters: dict[str, str] | None = None,
        session_id: str = "",
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Start a new process instance.

        ``cwd`` records where the instance's work happens (e.g. a git
        worktree); gate and action commands for this instance run there
        instead of the engine's project root. Defaults to the project root.
        """
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
        work_dir = ""
        if cwd:
            # Reject rather than fall back: a typo'd or relative cwd that
            # silently degraded to the project root would reproduce the
            # exact wrong-directory gate runs this parameter exists to fix,
            # while status output claimed otherwise. Relative paths would
            # resolve against the engine process's cwd, not the caller's.
            cwd_path = Path(cwd)
            if not cwd_path.is_absolute():
                raise ValueError(
                    f"cwd must be an absolute path, got '{cwd}'"
                )
            if not cwd_path.is_dir():
                raise ValueError(
                    f"cwd is not an existing directory: '{cwd}'"
                )
            work_dir = str(cwd_path.resolve())
        instance = self._store.create(
            process_name=defn.name,
            initial_state=initial.id,
            version=defn.version,
            definition_hash=def_hash,
            parameters=params,
            project_dir=work_dir,
        )
        self._emit(
            "started",
            instance,
            {
                "initial_state": initial.id,
                "parameters": params,
                "process_version": defn.version,
                "definition_hash": def_hash,
                "started_by": instance.started_by,
            },
            session_id=session_id,
        )
        self._store.save(instance)

        response = {
            "instance_id": instance.instance_id,
            "process_name": instance.process_name,
            "current_state": instance.current_state,
            "available_transitions": initial.transitions,
            "parameters": instance.parameters,
        }
        if instance.project_dir:
            response["project_dir"] = instance.project_dir
        return response

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
            if instance.project_dir:
                info["project_dir"] = instance.project_dir
            if state and state.role:
                info["role"] = state.role
            if state and state.agent_context:
                info["agent_context"] = state.agent_context.model_dump()
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
            if inst.project_dir:
                entry["project_dir"] = inst.project_dir
            if inst.suspended:
                entry["child_instance_id"] = inst.child_instance_id
            if inst.parent_instance_id:
                entry["parent_instance_id"] = inst.parent_instance_id
            result.append(entry)
        return result

    def _gate_params(
        self, instance: ProcessInstance, defn: ProcessDefinition,
    ) -> dict[str, str]:
        """Build the parameter set exposed to gate and action shells.

        Restricted to parameter names DECLARED by the definition, plus the
        engine-internal instance_id. Gate and action commands run in a shell
        with these exported as environment variables (see
        validator.run_command). Values are safe by construction because they
        are never interpolated into the command text; this method closes the
        matching name channel, so a caller cannot smuggle an undeclared,
        environment-significant name (PATH, LD_PRELOAD, IFS, BASH_ENV, ...)
        into the gate shell by passing it as an extra parameter. The name
        boundary is the set of names the definition author declared.
        """
        declared = {p.name for p in defn.parameters}
        params = {
            key: value
            for key, value in instance.parameters.items()
            if key in declared
        }
        params["instance_id"] = instance.instance_id
        return params

    def _work_dir(self, instance: ProcessInstance) -> Path:
        """Directory where this instance's gate/action commands run.

        The directory recorded at start (e.g. a git worktree) when it still
        exists, else the engine's project root. Without this, an instance
        started in a worktree had its `git diff`/test gates run against the
        main checkout, failing on state the worktree actually satisfies.
        """
        if instance.project_dir:
            recorded = Path(instance.project_dir)
            if recorded.is_dir():
                return recorded
        return self.project_root

    async def _run_exit_gates(
        self,
        state: ProcessState,
        validation_params: dict[str, str],
        work_dir: Path | None = None,
    ) -> tuple[list[ValidationResult], bool]:
        """Run a state's on_exit validations.

        Returns (results, blocked). Shared by transition and receive_signal so
        that a signal-driven state change is gated identically to an ordinary
        one; before this was factored out, the signal path ran no gates at all.
        """
        if not (state.on_exit and state.on_exit.validations):
            return [], False
        results = await run_validations(
            state.on_exit.validations, validation_params,
            work_dir or self.project_root,
        )
        return results, has_blocking_failures(results)

    async def _run_enter_gates(
        self,
        state: ProcessState,
        validation_params: dict[str, str],
        work_dir: Path | None = None,
    ) -> tuple[list[ValidationResult], bool]:
        """Run a state's on_enter validations. See _run_exit_gates."""
        if not (state.on_enter and state.on_enter.validations):
            return [], False
        results = await run_validations(
            state.on_enter.validations, validation_params,
            work_dir or self.project_root,
        )
        return results, has_blocking_failures(results)

    async def _run_actions(
        self,
        state: ProcessState,
        phase: str,
        instance: ProcessInstance,
        validation_params: dict[str, str],
        session_id: str = "",
    ) -> bool:
        """Run a state's on_enter/on_exit actions (non-blocking side effects).

        ``phase`` is ``"on_exit"`` or ``"on_enter"``. Shared by transition and
        receive_signal so a signal-driven state change runs the same actions
        as an ordinary one.

        Actions never block the state change, but a failure is recorded, not
        swallowed: a non-zero exit or an execution error emits an
        ``action_failed`` event to the stream and appends a line to log.txt.
        Actions are side effects in the audit layer itself (notifications,
        markers); a failure that vanishes without trace is data loss. The
        recording itself is fail-open, matching _emit: an unwritable log.txt
        must not abort a state change either.

        Returns True if any ``action_failed`` event was emitted, so a caller
        whose operation is later rejected can still persist the advanced
        event_seq (see transition's enter-gate block path).
        """
        hooks = state.on_exit if phase == "on_exit" else state.on_enter
        if not (hooks and hooks.actions):
            return False
        emitted = False
        for action in hooks.actions:
            exit_code: int | None = None
            output = ""
            error = ""
            try:
                output, exit_code = await run_command(
                    action.command, self._work_dir(instance), timeout=60,
                    parameters=validation_params, merge_stderr=True,
                )
            except (TimeoutError, OSError, ValueError) as exc:
                # ValueError covers spawn-level rejects such as an embedded
                # NUL in the command text; actions must stay non-blocking.
                error = f"{type(exc).__name__}: {exc}"
            if exit_code == 0:
                continue
            emitted = True
            self._emit(
                "action_failed",
                instance,
                payload={
                    "phase": phase,
                    "state": state.id,
                    "command": action.command[:500],
                    "exit_code": exit_code,
                    "error": error[:500],
                    "output": output[-500:],
                },
                session_id=session_id,
            )
            detail = f"exit {exit_code}" if exit_code is not None else error
            log_command = " ".join(action.command.split())
            # append_log is fail-open at the store level; an unwritable
            # log.txt cannot abort the state change.
            self._store.append_log(
                f"ACTION FAILED {instance.process_name}-"
                f"{instance.instance_id} {phase} {state.id} "
                f"({detail}): {log_command}"
            )
        return emitted

    async def _enter_state(
        self,
        instance: ProcessInstance,
        defn: ProcessDefinition,
        target: ProcessState | None,
        history_entry: HistoryEntry,
        all_results: list[ValidationResult],
        validation_params: dict[str, str],
        session_id: str = "",
        *,
        signal_name: str | None = None,
        signal_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TransitionResult:
        """Commit entry into ``history_entry.to_state``.

        The one canonical state-entry path, shared by transition() and
        receive_signal(): owns the instance mutation (current_state +
        history), on_enter actions, the per-state-type branch (dispatch /
        wait / subprocess / terminal / normal), event emission, logging,
        persistence, and result assembly.

        ``signal_name`` switches the path into signal semantics: events use
        the ``signal_received`` type carrying the signal and its data, log
        lines use the SIGNAL style, and dispatch parameter forwarding draws
        from the signal data instead of the transition metadata. Preserving
        pre-refactor behavior, the signal path also does NOT handle wait or
        subprocess targets specially and does NOT resume a suspended parent
        or fire on_complete when entering a terminal state; those
        asymmetries are tracked as GH-41 rather than silently changed here.
        ``target`` may be None only on the signal path (a transitions list
        naming a state missing from the definition), which degrades to the
        plain-entry branch exactly as before the extraction.
        """
        target_state = history_entry.to_state
        via_signal = signal_name is not None

        instance.current_state = target_state
        instance.history.append(history_entry)

        if target is not None:
            await self._run_actions(
                target, "on_enter", instance, validation_params, session_id
            )

        base_event_type = "signal_received" if via_signal else "transition"

        def base_payload(entry: HistoryEntry) -> dict[str, Any]:
            payload = self._entry_payload(entry)
            if via_signal:
                payload["signal"] = signal_name
                payload["data"] = signal_data
            return payload

        # Dispatch: create the child, then continue to the immediate target
        if target is not None and target.type == StateType.dispatch:
            extra_src: dict[str, Any] = (
                (signal_data or {}) if via_signal else (metadata or {})
            )
            extra_params = {
                k: str(v) for k, v in extra_src.items()
                if isinstance(v, (str, int, float, bool))
            }
            child_result = self._dispatch_child(
                instance, target, extra_params=extra_params
            )
            immediate_state = defn.get_state(target.immediate)
            if immediate_state is None and not via_signal:
                raise TransitionError(
                    f"Dispatch immediate target '{target.immediate}' "
                    f"not found in definition"
                )

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
            self._emit(
                base_event_type,
                instance,
                base_payload(history_entry),
                session_id=session_id,
            )
            self._emit(
                "dispatch",
                instance,
                {
                    **self._entry_payload(auto_entry),
                    "child_instance_id": child_result["instance_id"],
                    "child_process": child_result["process_name"],
                },
                session_id=session_id,
            )
            self._store.save(instance)

            if via_signal:
                self._store.append_log(
                    f"SIGNAL {instance.process_name}-{instance.instance_id}: "
                    f"received '{signal_name}', dispatched "
                    f"{child_result['process_name']}-"
                    f"{child_result['instance_id']}, "
                    f"continued to {target.immediate}"
                )
            else:
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
                available_transitions=(
                    immediate_state.transitions if immediate_state else []
                ),
                role=immediate_state.role if immediate_state else "",
                agent_context=(
                    immediate_state.agent_context.model_dump()
                    if immediate_state and immediate_state.agent_context
                    else None
                ),
                message=(
                    f"Dispatched '{target.process}' as instance "
                    f"{child_result['instance_id']}, "
                    f"continued to '{target.immediate}'"
                ),
                subprocess_started=child_result["instance_id"],
            )

        # Wait: mark the instance waiting (ordinary transitions only)
        if not via_signal and target is not None and target.type == StateType.wait:
            instance.waiting = True
            self._emit(
                "transition",
                instance,
                {
                    **self._entry_payload(history_entry),
                    "waiting": True,
                    "signal": target.signal.name,
                },
                session_id=session_id,
            )
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

        # Subprocess: start the child and suspend (ordinary transitions only)
        if not via_signal and target is not None and target.type == StateType.subprocess:
            child_result = self._start_subprocess(instance, target)
            self._emit(
                "transition",
                instance,
                self._entry_payload(history_entry),
                session_id=session_id,
            )
            self._emit(
                "subprocess_started",
                instance,
                {
                    "child_instance_id": child_result["instance_id"],
                    "child_process": child_result["process_name"],
                    "suspended": True,
                },
                session_id=session_id,
            )
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

        # Terminal and plain states share the log/result tail below
        parent_info = None
        if target is not None and target.type == StateType.terminal:
            self._emit(
                base_event_type,
                instance,
                base_payload(history_entry),
                session_id=session_id,
            )
            complete_payload: dict[str, Any] = {"final_state": target_state}
            if via_signal:
                complete_payload["via"] = "signal"
            self._emit(
                "complete",
                instance,
                complete_payload,
                session_id=session_id,
            )
            self._store.complete(instance)
            if not via_signal:
                # Check if this child completing should resume a parent
                parent_info = self._resume_parent(
                    instance, "completed", child_terminal_state=target_state,
                    session_id=session_id,
                )
                # Fire on_complete notification
                await self._notify("on_complete", {
                    "name": instance.process_name,
                    "instance_id": instance.instance_id,
                    "state": target_state,
                })
        else:
            self._emit(
                base_event_type,
                instance,
                base_payload(history_entry),
                session_id=session_id,
            )
            self._store.save(instance)

        if via_signal:
            self._store.append_log(
                f"SIGNAL {instance.process_name}-{instance.instance_id}: "
                f"received '{signal_name}', transitioned to {target_state}"
            )
        else:
            self._store.append_log(
                f"TRANSITION {instance.process_name}-{instance.instance_id}: "
                f"{history_entry.from_state} -> {target_state}"
            )

        result = TransitionResult(
            success=True,
            new_state=target_state,
            validation_results=[_vr_to_dict(r) for r in all_results],
            available_transitions=target.transitions if target else [],
            role=target.role if target else "",
            agent_context=(
                target.agent_context.model_dump()
                if target and target.agent_context else None
            ),
            skill_directives=[
                {"skill": sd.skill, "args": sd.args}
                for sd in (target.skill_directives if target else [])
            ],
            required_metadata=[
                {"key": rm.key, "description": rm.description}
                for rm in (target.required_metadata if target else [])
            ],
        )

        if target is not None and target.type == StateType.terminal:
            result.summary = _compute_summary(instance)

        if parent_info:
            result.parent_resumed = True
            result.parent_instance_id = parent_info["parent_instance_id"]
            result.parent_available_transitions = parent_info["available_transitions"]

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

        # Build validation context: declared params + instance metadata.
        # Restricted to declared names so a caller cannot inject
        # environment-significant names into the gate shell (see _gate_params).
        validation_params = self._gate_params(instance, defn)

        # Run on_exit validations for current state
        exit_results, exit_blocked = await self._run_exit_gates(
            current, validation_params, work_dir=self._work_dir(instance)
        )
        all_results.extend(exit_results)
        if exit_blocked:
            return TransitionResult(
                success=False,
                new_state=instance.current_state,
                validation_results=[_vr_to_dict(r) for r in all_results],
                available_transitions=current.transitions,
                message="on_exit validation failed",
            )

        # Run on_exit actions for current state
        actions_emitted = await self._run_actions(
            current, "on_exit", instance, validation_params, session_id
        )

        # Run on_enter validations for target state
        enter_results, enter_blocked = await self._run_enter_gates(
            target, validation_params, work_dir=self._work_dir(instance)
        )
        all_results.extend(enter_results)
        if enter_blocked:
            if actions_emitted:
                # An action_failed event consumed sequence numbers but the
                # transition will not persist. Save the (otherwise
                # unmutated) instance so the advanced event_seq lands and a
                # retried transition cannot re-emit at the same seq, which
                # would shadow the failure record under the consumers'
                # dedupe-keep-last rule.
                self._store.save(instance)
            return TransitionResult(
                success=False,
                new_state=instance.current_state,
                validation_results=[_vr_to_dict(r) for r in all_results],
                available_transitions=current.transitions,
                message="on_enter validation failed",
            )

        # Transition succeeds: commit through the canonical entry path
        history_entry = HistoryEntry(**{
            "from": instance.current_state,
            "to": target_state,
            "at": _now_iso(),
            "role": target.role,
            "session_id": session_id,
            "validations": [_vr_to_dict(r) for r in all_results],
            "metadata": metadata or {},
        })
        return await self._enter_state(
            instance, defn, target, history_entry, all_results,
            validation_params, session_id, metadata=metadata,
        )

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
            # Delegated work happens where the parent's work happens: a
            # parent started in a worktree must not have its child's gates
            # run against the engine's project root.
            project_dir=parent.project_dir,
        )

        # Link parent and child
        child_instance.parent_instance_id = parent.instance_id
        child_instance.parent_state_id = state.id
        self._emit(
            "started",
            child_instance,
            {
                "initial_state": initial.id,
                "parameters": child_params,
                "process_version": child_defn.version,
                "definition_hash": child_hash,
                "parent_instance_id": parent.instance_id,
                "parent_state_id": state.id,
            },
        )
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
            project_dir=parent.project_dir,
        )

        # Link child to parent (but don't suspend parent)
        child_instance.parent_instance_id = parent.instance_id
        child_instance.parent_state_id = state.id
        if state.assign_to:
            child_instance.started_by = state.assign_to
        self._emit(
            "started",
            child_instance,
            {
                "initial_state": initial.id,
                "parameters": child_params,
                "process_version": child_defn.version,
                "definition_hash": child_hash,
                "parent_instance_id": parent.instance_id,
                "parent_state_id": state.id,
                "assigned_to": state.assign_to or "",
            },
        )
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
        session_id: str = "",
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
        # "parent_resumed" extends the issue's event list: resuming a
        # suspended parent is a mutation not expressible by any other event.
        self._emit(
            "parent_resumed",
            parent,
            {
                "child_instance_id": child.instance_id,
                "child_process": child.process_name,
                "outcome": outcome,
                "available_transitions": available,
            },
            session_id=session_id,
        )
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
            triggered_by=session_id,
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

        self._emit(
            "skip",
            instance,
            {**self._entry_payload(history_entry), "reason": reason},
            session_id=session_id,
        )
        if target.type == StateType.terminal:
            self._emit(
                "complete",
                instance,
                {"final_state": target_state, "via": "skip"},
                session_id=session_id,
            )
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

    async def receive_signal(
        self, instance_id: str, signal_name: str,
        data: dict[str, Any],
        target_state: str | None = None,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Deliver a signal to a waiting process instance.

        Validates the signal name and required fields, stores the signal
        data, and optionally transitions to a target state.

        A transition requested here runs the same validation gates as an
        ordinary transition. If a blocking gate fails, the call is a no-op
        mirroring a blocked ordinary transition: nothing is written, the
        instance stays in the wait state (waiting is NOT cleared, the signal is
        NOT consumed), and the result reports success=False with
        still_waiting=True. The same signal can then be delivered again through
        this method once the gate's condition is met, which re-runs the gates
        and completes the transition with the signal's attribution intact. Do
        not advance a refused instance with transition(); that would drop the
        signal name and data from the recorded history.

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

            # Gate the signal-driven transition exactly as an ordinary one.
            # Run gates BEFORE mutating the instance so a blocked signal is a
            # true no-op: the wait is not cleared and no state is written, so
            # the same signal can be re-delivered (re-running the gates) once
            # the gate condition is met, and the eventual successful transition
            # still carries the signal name and its data as attribution.
            validation_params = self._gate_params(instance, defn)
            gate_results: list[ValidationResult] = []
            exit_results, exit_blocked = await self._run_exit_gates(
                current, validation_params, work_dir=self._work_dir(instance)
            )
            gate_results.extend(exit_results)
            blocked_by = "on_exit" if exit_blocked else ""
            if not exit_blocked and target is not None:
                enter_results, enter_blocked = await self._run_enter_gates(
                    target, validation_params,
                    work_dir=self._work_dir(instance),
                )
                gate_results.extend(enter_results)
                if enter_blocked:
                    blocked_by = "on_enter"

            if blocked_by:
                # Refuse the state change and leave the instance untouched
                # (still waiting), mirroring a blocked ordinary transition.
                self._store.append_log(
                    f"SIGNAL {instance.process_name}-{instance.instance_id}: "
                    f"received '{signal_name}', transition to {target_state} "
                    f"refused ({blocked_by} validation failed); still waiting"
                )
                result["success"] = False
                result["new_state"] = instance.current_state
                result["available_transitions"] = []
                result["still_waiting"] = True
                result["validation_results"] = [
                    _vr_to_dict(r) for r in gate_results
                ]
                result["message"] = f"{blocked_by} validation failed"
                return result

            # Gates passed: now commit the signal and clear the wait.
            instance.signal_data = data
            instance.waiting = False
            await self._run_actions(
                current, "on_exit", instance, validation_params, session_id
            )

            if gate_results:
                result["validation_results"] = [
                    _vr_to_dict(r) for r in gate_results
                ]

            history_entry = HistoryEntry(**{
                "from": instance.current_state,
                "to": target_state,
                "at": _now_iso(),
                "triggered_by": f"signal: {signal_name}",
                "role": target.role if target else "",
                "session_id": session_id,
                "metadata": {"signal_data": data},
            })

            # Commit through the canonical entry path (signal semantics:
            # signal_received events, SIGNAL log lines, dispatch params
            # from the signal data). The dict below keeps its historical
            # key set, so only the fields it always carried are copied.
            tr = await self._enter_state(
                instance, defn, target, history_entry, [],
                validation_params, session_id,
                signal_name=signal_name, signal_data=data,
            )
            result["new_state"] = tr.new_state
            result["available_transitions"] = tr.available_transitions
            result["role"] = tr.role
            if tr.subprocess_started:
                result["subprocess_started"] = tr.subprocess_started
        else:
            # Signal received but no transition yet; record it and unlock
            # transitions. There is no target state, so there are no gates to
            # run and nothing to refuse.
            instance.signal_data = data
            instance.waiting = False
            self._emit(
                "signal_received",
                instance,
                {
                    "signal": signal_name,
                    "data": data,
                    "awaiting_transition": True,
                },
                session_id=session_id,
            )
            self._store.save(instance)

            self._store.append_log(
                f"SIGNAL {instance.process_name}-{instance.instance_id}: "
                f"received '{signal_name}', awaiting transition"
            )

            result["new_state"] = instance.current_state
            result["available_transitions"] = current.transitions

        return result

    def abandon(
        self, instance_id: str, reason: str, session_id: str = ""
    ) -> dict[str, Any]:
        """Abandon a process instance."""
        instance = self._store.load(instance_id)
        final_state = instance.current_state
        self._emit(
            "abandon",
            instance,
            {"final_state": final_state, "reason": reason},
            session_id=session_id,
        )
        self._store.abandon(instance, reason)

        result: dict[str, Any] = {
            "success": True,
            "final_state": final_state,
            "reason": reason,
        }

        # If this was a child process, resume the parent
        parent_info = self._resume_parent(
            instance, "abandoned", session_id=session_id
        )
        if parent_info:
            result["parent_resumed"] = True
            result["parent_instance_id"] = parent_info["parent_instance_id"]
            result["parent_available_transitions"] = parent_info["available_transitions"]

        return result

    def undo(
        self, instance_id: str, reason: str, session_id: str = ""
    ) -> TransitionResult:
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
        self._emit(
            "undo",
            instance,
            {
                "removed_state": last.to_state,
                "restored_state": previous_state,
                "reason": reason,
            },
            session_id=session_id,
        )
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
        self, instance_id: str, to_user: str, reason: str,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Log an ownership transfer (metadata only)."""
        instance = self._store.load(instance_id)
        previous_owner = instance.started_by
        instance.started_by = to_user
        self._emit(
            "handoff",
            instance,
            {
                "from_user": previous_owner,
                "to_user": to_user,
                "reason": reason,
            },
            session_id=session_id,
            actor=to_user,
        )
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
            if h.role:
                entry["role"] = h.role
            if h.session_id:
                entry["session_id"] = h.session_id
            if h.metadata:
                entry["metadata"] = h.metadata
            result.append(entry)
        return result

    def trajectory(self, instance_id: str) -> dict[str, Any]:
        """Full recorded trajectory of an instance, for human review.

        Unlike history(), includes the instance envelope (status, current
        state, parameters, actor attribution, timestamps) and the override
        log, so a reviewer sees the whole record rather than transitions
        alone. Searches active, completed, and abandoned instances.
        """
        instance = self._store.load_any(instance_id)
        transitions: list[dict[str, Any]] = []
        for h in instance.history:
            transitions.append(
                {
                    "from_state": h.from_state,
                    "to_state": h.to_state,
                    "timestamp": h.at,
                    "triggered_by": h.triggered_by,
                    "role": h.role,
                    "session_id": h.session_id,
                    "validations": h.validations,
                    "metadata": h.metadata,
                }
            )
        return {
            "instance_id": instance.instance_id,
            "process_name": instance.process_name,
            "process_version": instance.process_version,
            "status": instance.status,
            "current_state": instance.current_state,
            "started_at": instance.started_at,
            "updated_at": instance.updated_at,
            "started_by": instance.started_by,
            "parameters": instance.parameters,
            "waiting": instance.waiting,
            "signal_data": instance.signal_data,
            "parent_instance_id": instance.parent_instance_id,
            "parent_state_id": instance.parent_state_id,
            "child_instance_id": instance.child_instance_id,
            "suspended": instance.suspended,
            "transitions": transitions,
            "overrides": [o.model_dump() for o in instance.overrides],
        }

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
        """Validate a process definition file.

        Schema/reference problems fail the load and come back as errors.
        A loadable definition additionally gets the static graph pass:
        unreachable states and dead-end sinks (states with no path to any
        terminal) are surfaced as warnings, since they load fine but fail
        or mislead at runtime.
        """
        try:
            defn = load_definition(Path(path))
            analysis = graph_analyze(defn)
            return {
                "valid": not analysis.errors,
                "name": defn.name,
                "version": defn.version,
                "states": [s.id for s in defn.states],
                "errors": analysis.errors,
                "warnings": analysis.warnings,
            }
        except Exception as e:
            return {
                "valid": False,
                "errors": [str(e)],
                "warnings": [],
            }
