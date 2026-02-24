"""Inheritance resolution: merge child overrides onto parent definitions."""

from __future__ import annotations

import copy
from typing import Any

from turnstile_core.errors import InheritanceError
from turnstile_core.models import (
    ActionHook,
    OverrideSpec,
    ProcessDefinition,
    ProcessOverride,
    ProcessState,
    StateHooks,
    StateHooksPatch,
    StatePatch,
    ValidationPatch,
)


def resolve_inheritance(
    override: ProcessOverride,
    parent: ProcessDefinition,
) -> ProcessDefinition:
    """Merge a child override onto a parent definition.

    Returns a new ProcessDefinition with the overrides applied.
    """
    # Deep-copy parent so we don't mutate the original
    states = [s.model_copy(deep=True) for s in parent.states]
    parameters = [p.model_copy(deep=True) for p in parent.parameters]

    spec = override.overrides

    # 1. Add new states
    existing_ids = {s.id for s in states}
    for new_state in spec.add_states:
        if new_state.id in existing_ids:
            raise InheritanceError(
                f"Cannot add state '{new_state.id}': already exists in parent"
            )
        states.append(new_state.model_copy(deep=True))
        existing_ids.add(new_state.id)

    # 2. Patch existing states
    state_map = {s.id: s for s in states}
    for patch in spec.patch_states:
        if patch.id not in state_map:
            raise InheritanceError(
                f"Cannot patch state '{patch.id}': not found in parent"
            )
        _apply_state_patch(state_map[patch.id], patch)

    # 3. Apply parameter overrides
    parameters = _apply_parameter_overrides(parameters, spec.parameters)

    # 4. Build the merged definition
    data: dict[str, Any] = {
        "name": override.name or parent.name,
        "description": override.description if override.description is not None else parent.description,
        "version": override.version or parent.version,
        "extends": override.extends,
        "metadata": parent.metadata.model_dump(),
        "parameters": [p.model_dump() for p in parameters],
        "states": [s.model_dump(by_alias=True) for s in states],
    }

    try:
        return ProcessDefinition(**data)
    except Exception as e:
        raise InheritanceError(
            f"Merged definition is invalid: {e}"
        ) from e


def _apply_state_patch(state: ProcessState, patch: StatePatch) -> None:
    """Apply a patch to a state in place."""
    if patch.transitions is not None:
        state.transitions = patch.transitions

    if patch.description is not None:
        state.description = patch.description

    if patch.on_enter is not None:
        state.on_enter = _apply_hooks_patch(state.on_enter, patch.on_enter)

    if patch.on_exit is not None:
        state.on_exit = _apply_hooks_patch(state.on_exit, patch.on_exit)

    if patch.metadata is not None:
        state.metadata = {**state.metadata, **patch.metadata}


def _apply_hooks_patch(
    existing: StateHooks | None,
    patch: StateHooksPatch,
) -> StateHooks:
    """Apply a hooks patch, returning the new hooks."""
    if existing is None:
        existing = StateHooks()

    if patch.validations is not None:
        existing = _apply_validation_patch(existing, patch.validations)

    if patch.actions is not None:
        existing.actions = [a.model_copy(deep=True) for a in patch.actions]

    return existing


def _apply_validation_patch(
    hooks: StateHooks,
    patch: ValidationPatch,
) -> StateHooks:
    """Apply a validation patch to hooks."""
    if patch.mode == "replace":
        hooks.validations = list(patch.items)
    elif patch.mode == "append":
        hooks.validations = list(hooks.validations) + list(patch.items)
    else:
        raise InheritanceError(
            f"Invalid validation patch mode: '{patch.mode}' "
            f"(expected 'append' or 'replace')"
        )
    return hooks


def _apply_parameter_overrides(
    parameters: list,
    overrides,
) -> list:
    """Apply parameter append/remove overrides."""
    # Remove parameters
    if overrides.remove:
        remove_set = set(overrides.remove)
        parameters = [p for p in parameters if p.name not in remove_set]

    # Append new parameters
    existing_names = {p.name for p in parameters}
    for new_param in overrides.append:
        if new_param.name in existing_names:
            raise InheritanceError(
                f"Cannot append parameter '{new_param.name}': "
                f"already exists (remove it first to replace)"
            )
        parameters.append(new_param.model_copy(deep=True))

    return parameters
