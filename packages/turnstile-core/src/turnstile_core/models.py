"""Pydantic models for turnstile process definitions."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class Severity(str, Enum):
    error = "error"
    warning = "warning"
    info = "info"


class StateType(str, Enum):
    initial = "initial"
    terminal = "terminal"
    normal = "normal"
    subprocess = "subprocess"


# ---------------------------------------------------------------------------
# Validation expressions
# ---------------------------------------------------------------------------

# Regex patterns for parsing expect expressions
_BARE_KEYWORDS = {"empty", "not_empty"}
_FUNC_PATTERN = re.compile(
    r'^(equals|not_equals|contains|starts_with|ends_with|matches)\("(.*)"\)$'
)
_NUMERIC_PATTERN = re.compile(r"^(greater_than|less_than)\((\d+)\)$")
_EXIT_CODE_PATTERN = re.compile(r"^exit_code\((\d+)\)$")


def parse_expect(expr: str) -> dict[str, Any]:
    """Parse an expect expression string into a structured dict.

    Returns a dict with 'type' and relevant parameters. This is used
    for serialization/inspection. The actual checking logic lives in
    validator.py.
    """
    expr = expr.strip()

    if expr in _BARE_KEYWORDS:
        return {"type": expr}

    m = _FUNC_PATTERN.match(expr)
    if m:
        return {"type": m.group(1), "value": m.group(2)}

    m = _NUMERIC_PATTERN.match(expr)
    if m:
        return {"type": m.group(1), "value": int(m.group(2))}

    m = _EXIT_CODE_PATTERN.match(expr)
    if m:
        return {"type": "exit_code", "value": int(m.group(1))}

    raise ValueError(f"Invalid expect expression: {expr!r}")


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


class ProcessState(BaseModel):
    """A single state in the process state machine."""

    id: str
    description: str = ""
    type: StateType = StateType.normal
    transitions: list[str] = Field(default_factory=list)
    on_enter: StateHooks | None = None
    on_exit: StateHooks | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Subprocess-specific fields
    process: str | None = None
    parameter_map: dict[str, str] | None = None


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

        # Terminal states must not have transitions
        for state in self.states:
            if state.type == StateType.terminal and state.transitions:
                raise ValueError(
                    f"Terminal state '{state.id}' must not have transitions"
                )

        return self

    def get_state(self, state_id: str) -> ProcessState | None:
        """Look up a state by ID."""
        for s in self.states:
            if s.id == state_id:
                return s
        return None

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


class RegistrySettings(BaseModel):
    """Global settings from registry.yaml."""

    state_dir: str = ".process-state"
    log_retention_days: int = 90
    require_override_reason: bool = True
    notifications: dict[str, str] = Field(default_factory=dict)


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
