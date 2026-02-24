"""Custom exceptions for the turnstile process engine."""


class TurnstileError(Exception):
    """Base exception for all turnstile errors."""


class DefinitionError(TurnstileError):
    """Error in a process definition (invalid YAML, bad schema, etc.)."""


class TransitionError(TurnstileError):
    """Illegal state transition attempted."""


class ValidationError(TurnstileError):
    """A validation gate failed with severity=error."""


class InstanceNotFoundError(TurnstileError):
    """Referenced process instance does not exist."""


class ProcessNotFoundError(TurnstileError):
    """Referenced process definition does not exist."""


class RegistryResolutionError(TurnstileError):
    """Failed to resolve an external process source."""


class InheritanceError(TurnstileError):
    """Error resolving process definition inheritance."""


class SubprocessError(TurnstileError):
    """Error in subprocess delegation."""
