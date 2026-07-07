"""Pydantic models for turnstile process definitions."""

from __future__ import annotations

from enum import Enum
from functools import cached_property
from typing import Any

from pydantic import BaseModel, Field, model_validator

from turnstile_core.definition.expect import parse_expect


class Severity(str, Enum):
    error = "error"
    warning = "warning"
    info = "info"


class StateType(str, Enum):
    initial = "initial"
    terminal = "terminal"
    normal = "normal"
    subprocess = "subprocess"
    wait = "wait"
    dispatch = "dispatch"


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------


class ValidationRule(BaseModel):
    """A single validation gate: run a command and check the output."""

    command: str
    expect: str
    message: str = ""
    severity: Severity = Severity.error
    timeout: int = 60
    evidence: str | None = None
    max_age: str | None = None

    @model_validator(mode="after")
    def _check_expect_parses(self) -> ValidationRule:
        parse_expect(self.expect)
        return self


class CompositeValidation(BaseModel):
    """Composite assertion: 'any' (OR) or 'all' (AND) over a list of rules."""

    any_of: list[ValidationRule] | None = Field(None, alias="any")
    all_of: list[ValidationRule] | None = Field(None, alias="all")
    message: str = ""

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _exactly_one(self) -> CompositeValidation:
        has_any = self.any_of is not None
        has_all = self.all_of is not None
        if has_any == has_all:
            raise ValueError(
                "CompositeValidation must have exactly one of 'any' or 'all'"
            )
        return self


# A validation entry can be a simple rule or a composite
ValidationEntry = ValidationRule | CompositeValidation


# ---------------------------------------------------------------------------
# State hooks (on_enter / on_exit)
# ---------------------------------------------------------------------------


class ActionHook(BaseModel):
    """A shell command to run as a side effect (not a gate)."""

    command: str


class StateHooks(BaseModel):
    """Hooks that run on entering or exiting a state."""

    validations: list[ValidationEntry] = Field(
        default_factory=list, alias="validate"
    )
    actions: list[ActionHook] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Process states
# ---------------------------------------------------------------------------


class SubprocessRouting(BaseModel):
    """Routing targets for subprocess state completion/failure.

    on_complete can be:
      - list[str]: available transitions when child reaches any terminal state
      - dict[str, str]: maps child terminal state name to parent target state.
        A '_default' key provides fallback; if missing and child state not in
        dict, all dict values become available transitions.
    """

    on_complete: list[str] | dict[str, str] = Field(default_factory=list)
    on_fail: list[str] = Field(default_factory=list)

    def on_complete_targets(self) -> list[str]:
        """Return all possible on_complete target state names."""
        if isinstance(self.on_complete, dict):
            return list(set(self.on_complete.values()))
        return self.on_complete

    def resolve_on_complete(self, child_terminal_state: str) -> list[str]:
        """Resolve available transitions given the child's terminal state.

        For list form, returns the list as-is.
        For dict form, looks up the child state, falls back to _default,
        then falls back to all values.
        """
        if isinstance(self.on_complete, dict):
            if child_terminal_state in self.on_complete:
                return [self.on_complete[child_terminal_state]]
            if "_default" in self.on_complete:
                return [self.on_complete["_default"]]
            return list(set(self.on_complete.values()))
        return self.on_complete


class SkillDirective(BaseModel):
    """A Claude Code skill to invoke when entering a state."""

    skill: str
    args: str = ""


class RequiredMetadata(BaseModel):
    """A metadata key required on transitions out of a state."""

    key: str
    description: str = ""


class SignalField(BaseModel):
    """A required field in a signal payload."""

    key: str
    description: str = ""


class SignalSpec(BaseModel):
    """Specification for a signal that a wait state expects."""

    name: str
    required_fields: list[SignalField] = Field(default_factory=list)


class AgentContext(BaseModel):
    """Configuration for specializing an agent when entering a state.

    Surfaced in MCP responses so the agent runtime can configure itself.
    The engine does not enforce these; they are advisory. The process
    definition carries the expertise; the agent absorbs it on entry.
    """

    guidance: str = ""
    reference_files: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class StatePermissions(BaseModel):
    """Permissions governing what actions are allowed in a state.

    Controls what an agent may do while the process is in this state.
    The guard checks these on every mutation attempt. Defaults are
    permissive for backward compatibility; restrict to tighten.

    edit_paths restricts which files may be edited when edit is True.
    Uses glob patterns relative to the project root (e.g. "src/**").
    Empty list means all paths are allowed.

    Command execution is governed the same way: ``run`` gates shell
    commands wholesale; ``deny_commands`` blocks matching commands
    even when run is True; ``allow_commands``, if non-empty, permits
    only matching commands. Patterns are fnmatch globs tested against
    the full command string (e.g. "git push*", "*deploy*").
    """

    edit: bool = True
    edit_paths: list[str] = Field(default_factory=list)
    run: bool = True
    allow_commands: list[str] = Field(default_factory=list)
    deny_commands: list[str] = Field(default_factory=list)


class ProcessState(BaseModel):
    """A single state in the process state machine."""

    id: str
    description: str = ""
    type: StateType = StateType.normal
    role: str = ""
    agent_context: AgentContext | None = None
    transitions: list[str] = Field(default_factory=list)
    on_enter: StateHooks | None = None
    on_exit: StateHooks | None = None
    permissions: StatePermissions = Field(default_factory=StatePermissions)
    metadata: dict[str, Any] = Field(default_factory=dict)
    skill_directives: list[SkillDirective] = Field(default_factory=list)
    required_metadata: list[RequiredMetadata] = Field(default_factory=list)

    # Subprocess-specific fields
    process: str | None = None
    parameter_map: dict[str, str] | None = None
    subprocess_routing: SubprocessRouting | None = None

    # Wait-specific fields
    signal: SignalSpec | None = None

    # Dispatch-specific fields
    immediate: str | None = None
    assign_to: str | None = None

    @model_validator(mode="after")
    def _check_subprocess_fields(self) -> ProcessState:
        if self.type == StateType.subprocess:
            if not self.process:
                raise ValueError(
                    f"Subprocess state '{self.id}' must have 'process' field"
                )
            if self.transitions:
                raise ValueError(
                    f"Subprocess state '{self.id}' must not have 'transitions' "
                    f"(use subprocess_routing instead)"
                )
            if not self.subprocess_routing:
                raise ValueError(
                    f"Subprocess state '{self.id}' must have 'subprocess_routing'"
                )
        else:
            if self.subprocess_routing is not None:
                raise ValueError(
                    f"Non-subprocess state '{self.id}' must not have "
                    f"'subprocess_routing'"
                )
        return self

    @model_validator(mode="after")
    def _check_wait_fields(self) -> ProcessState:
        if self.type == StateType.wait:
            if not self.signal:
                raise ValueError(
                    f"Wait state '{self.id}' must have 'signal' field"
                )
            if not self.transitions:
                raise ValueError(
                    f"Wait state '{self.id}' must have transitions "
                    f"(targets for after signal delivery)"
                )
        else:
            if self.signal is not None:
                raise ValueError(
                    f"Non-wait state '{self.id}' must not have 'signal'"
                )
        return self

    @model_validator(mode="after")
    def _check_dispatch_fields(self) -> ProcessState:
        if self.type == StateType.dispatch:
            if not self.process:
                raise ValueError(
                    f"Dispatch state '{self.id}' must have 'process' field"
                )
            if not self.immediate:
                raise ValueError(
                    f"Dispatch state '{self.id}' must have 'immediate' field "
                    f"(target state after dispatch)"
                )
            if self.transitions:
                raise ValueError(
                    f"Dispatch state '{self.id}' must not have 'transitions' "
                    f"(uses 'immediate' instead)"
                )
        else:
            if self.immediate is not None:
                raise ValueError(
                    f"Non-dispatch state '{self.id}' must not have 'immediate'"
                )
        return self


# ---------------------------------------------------------------------------
# Process parameters
# ---------------------------------------------------------------------------


class ProcessParameter(BaseModel):
    """A parameter declared by a process definition."""

    name: str
    description: str = ""
    required: bool = True
    default: str | None = None


# ---------------------------------------------------------------------------
# Process definition (top-level)
# ---------------------------------------------------------------------------


class ProcessMetadata(BaseModel):
    """Optional metadata about a process definition."""

    author: str = ""
    tags: list[str] = Field(default_factory=list)
    estimated_duration: str = ""


class ProcessDefinition(BaseModel):
    """A complete process definition, parsed from YAML."""

    name: str
    description: str = ""
    version: str = "0.1.0"
    extends: str | None = None
    metadata: ProcessMetadata = Field(default_factory=ProcessMetadata)
    parameters: list[ProcessParameter] = Field(default_factory=list)
    states: list[ProcessState]

    @model_validator(mode="after")
    def _validate_structure(self) -> ProcessDefinition:
        state_ids = {s.id for s in self.states}

        # Must have exactly one initial state
        initial_states = [s for s in self.states if s.type == StateType.initial]
        if len(initial_states) != 1:
            raise ValueError(
                f"Process must have exactly one initial state, "
                f"found {len(initial_states)}"
            )

        # Must have at least one terminal state
        terminal_states = [s for s in self.states if s.type == StateType.terminal]
        if len(terminal_states) < 1:
            raise ValueError("Process must have at least one terminal state")

        # All transition targets must reference existing states
        for state in self.states:
            for target in state.transitions:
                if target not in state_ids:
                    raise ValueError(
                        f"State '{state.id}' has transition to unknown "
                        f"state '{target}'"
                    )

        # Subprocess routing targets must reference existing states
        for state in self.states:
            if state.subprocess_routing:
                for target in state.subprocess_routing.on_complete_targets():
                    if target not in state_ids:
                        raise ValueError(
                            f"Subprocess state '{state.id}' on_complete "
                            f"references unknown state '{target}'"
                        )
                for target in state.subprocess_routing.on_fail:
                    if target not in state_ids:
                        raise ValueError(
                            f"Subprocess state '{state.id}' on_fail "
                            f"references unknown state '{target}'"
                        )

        # Terminal states must not have transitions
        for state in self.states:
            if state.type == StateType.terminal and state.transitions:
                raise ValueError(
                    f"Terminal state '{state.id}' must not have transitions"
                )

        return self

    @cached_property
    def _states_by_id(self) -> dict[str, ProcessState]:
        return {s.id: s for s in self.states}

    def get_state(self, state_id: str) -> ProcessState | None:
        """Look up a state by ID."""
        return self._states_by_id.get(state_id)

    def initial_state(self) -> ProcessState:
        """Return the initial state."""
        for s in self.states:
            if s.type == StateType.initial:
                return s
        raise ValueError("No initial state found")


# ---------------------------------------------------------------------------
# Registry config
# ---------------------------------------------------------------------------


class RegistryExtend(BaseModel):
    """A shared process source in the registry."""

    source: str
    version: str = ""
    processes: list[str] = Field(default_factory=list)


class VerificationSettings(BaseModel):
    """Settings for acceptance-time verification (docs/guarantee.md).

    anchor_file / anchor_command must reference locations outside the
    agent's write domain, or anchoring proves nothing. The command
    receives {instance_id}, {head}, and {entries} substitutions.
    """

    record_git_sha: bool = True
    anchor_file: str = ""
    anchor_command: str = ""
    signal_key_file: str = ""


class RegistrySettings(BaseModel):
    """Global settings from registry.yaml."""

    state_dir: str = ".process-state"
    log_retention_days: int = 90
    require_override_reason: bool = True
    notifications: dict[str, str] = Field(default_factory=dict)
    enforcement: str = "off"  # off, monitor, enforce
    path_catalogue: dict[str, list[str]] = Field(default_factory=dict)
    verification: VerificationSettings = Field(
        default_factory=VerificationSettings
    )

    @model_validator(mode="after")
    def _validate_enforcement(self) -> RegistrySettings:
        if self.enforcement not in ("off", "monitor", "enforce"):
            raise ValueError(
                f"enforcement must be 'off', 'monitor', or 'enforce', "
                f"got '{self.enforcement}'"
            )
        return self


class RegistryConfig(BaseModel):
    """The .processes/registry.yaml file."""

    version: str = "1.0"
    extends: list[RegistryExtend] = Field(default_factory=list)
    local: list[str] = Field(default_factory=list)
    settings: RegistrySettings = Field(default_factory=RegistrySettings)


# ---------------------------------------------------------------------------
# Inheritance / override models
# ---------------------------------------------------------------------------


class ValidationPatch(BaseModel):
    """Patch to a state's validation list."""

    mode: str = "append"  # "append" or "replace"
    items: list[ValidationEntry] = Field(default_factory=list)


class StateHooksPatch(BaseModel):
    """Patch to a state's hooks (on_enter or on_exit)."""

    validations: ValidationPatch | None = Field(None, alias="validate")
    actions: list[ActionHook] | None = None

    model_config = {"populate_by_name": True}


class StatePatch(BaseModel):
    """Patch applied to an existing state in a parent definition."""

    id: str
    transitions: list[str] | None = None
    description: str | None = None
    role: str | None = None
    agent_context: AgentContext | None = None
    on_enter: StateHooksPatch | None = None
    on_exit: StateHooksPatch | None = None
    metadata: dict[str, Any] | None = None


class ParameterOverride(BaseModel):
    """Override section for parameters."""

    append: list[ProcessParameter] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class OverrideSpec(BaseModel):
    """The overrides block in an inheriting definition."""

    add_states: list[ProcessState] = Field(default_factory=list)
    patch_states: list[StatePatch] = Field(default_factory=list)
    parameters: ParameterOverride = Field(default_factory=ParameterOverride)


class ProcessOverride(BaseModel):
    """A process file that extends a parent with overrides.

    Parsed separately from ProcessDefinition because the states list
    comes from the parent, not this file.
    """

    extends: str
    version_constraint: str = ""
    name: str | None = None
    description: str | None = None
    version: str | None = None
    overrides: OverrideSpec = Field(default_factory=OverrideSpec)
