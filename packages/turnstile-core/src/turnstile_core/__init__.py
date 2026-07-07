"""Turnstile: a process engine for AI agents.

Layered layout (each layer only imports from the ones above it):

    errors, templating   -- leaf utilities
    definition/          -- what a process is (pure models + YAML loading)
    instance/            -- what a run is (records + file store)
    kernel/              -- pure transition semantics (no I/O)
    runtime/             -- the imperative shell (Engine facade, gates)
    ops/                 -- enforcement, guard adapters, git hooks, scaffolding
"""

from turnstile_core.definition import ProcessDefinition, load_definition
from turnstile_core.instance import ProcessInstance, StateStore
from turnstile_core.runtime import Engine, TransitionResult

__all__ = [
    "Engine",
    "ProcessDefinition",
    "ProcessInstance",
    "StateStore",
    "TransitionResult",
    "load_definition",
]
