"""What a process *is*: models, grammar, loading, inheritance, analysis.

Everything in this package describes process definitions — the
version-controlled YAML artifacts — independent of any particular run.
The only I/O here is reading definitions from disk (loader, registry).
"""

from turnstile_core.definition.expect import build_checker, parse_expect
from turnstile_core.definition.model import (
    AgentContext,
    CompositeValidation,
    ProcessDefinition,
    ProcessOverride,
    ProcessParameter,
    ProcessState,
    RegistryConfig,
    RegistrySettings,
    Severity,
    SignalSpec,
    StatePermissions,
    StateType,
    SubprocessRouting,
    ValidationEntry,
    ValidationRule,
)
from turnstile_core.definition.loader import (
    DiscoveredDefinition,
    definition_hash,
    discover_definitions,
    discover_definitions_full,
    is_override_file,
    load_definition,
    load_override,
    load_registry,
)

__all__ = [
    "AgentContext",
    "CompositeValidation",
    "DiscoveredDefinition",
    "ProcessDefinition",
    "ProcessOverride",
    "ProcessParameter",
    "ProcessState",
    "RegistryConfig",
    "RegistrySettings",
    "Severity",
    "SignalSpec",
    "StatePermissions",
    "StateType",
    "SubprocessRouting",
    "ValidationEntry",
    "ValidationRule",
    "build_checker",
    "definition_hash",
    "discover_definitions",
    "discover_definitions_full",
    "is_override_file",
    "load_definition",
    "load_override",
    "load_registry",
    "parse_expect",
]
