"""Load and validate process definitions from YAML files."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from turnstile_core.errors import DefinitionError, ProcessNotFoundError
from turnstile_core.models import ProcessDefinition, RegistryConfig


def definition_hash(path: Path) -> str:
    """Compute a SHA-256 hash of a definition file's content."""
    content = path.read_bytes()
    return f"sha256:{hashlib.sha256(content).hexdigest()[:12]}"


def load_definition(path: Path) -> ProcessDefinition:
    """Load and validate a single process definition YAML file.

    Raises DefinitionError if the file is invalid.
    """
    if not path.exists():
        raise DefinitionError(f"Definition file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise DefinitionError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise DefinitionError(f"Expected a YAML mapping in {path}, got {type(raw)}")

    try:
        return ProcessDefinition(**raw)
    except Exception as e:
        raise DefinitionError(f"Invalid process definition in {path}: {e}") from e


def load_registry(project_root: Path) -> RegistryConfig:
    """Load .processes/registry.yaml, returning defaults if it doesn't exist."""
    registry_path = project_root / ".processes" / "registry.yaml"
    if not registry_path.exists():
        return RegistryConfig()

    try:
        raw = yaml.safe_load(registry_path.read_text())
    except yaml.YAMLError as e:
        raise DefinitionError(f"Invalid YAML in {registry_path}: {e}") from e

    if raw is None:
        return RegistryConfig()

    try:
        return RegistryConfig(**raw)
    except Exception as e:
        raise DefinitionError(
            f"Invalid registry config in {registry_path}: {e}"
        ) from e


def discover_definitions(
    project_root: Path,
) -> dict[str, tuple[ProcessDefinition, str]]:
    """Discover all process definitions for a project.

    Returns a dict mapping process name to (definition, file_hash).

    Loads definitions listed in registry.yaml's `local` field, falling
    back to discovering all .yaml files in .processes/ (excluding
    registry.yaml and the overrides/ directory).
    """
    processes_dir = project_root / ".processes"
    if not processes_dir.exists():
        return {}

    registry = load_registry(project_root)
    result: dict[str, tuple[ProcessDefinition, str]] = {}

    if registry.local:
        # Load explicitly listed local processes
        for name in registry.local:
            path = processes_dir / f"{name}.yaml"
            if not path.exists():
                raise ProcessNotFoundError(
                    f"Local process '{name}' listed in registry but "
                    f"file not found: {path}"
                )
            defn = load_definition(path)
            result[defn.name] = (defn, definition_hash(path))
    else:
        # Auto-discover all .yaml files (excluding registry and overrides)
        for path in sorted(processes_dir.glob("*.yaml")):
            if path.name == "registry.yaml":
                continue
            defn = load_definition(path)
            result[defn.name] = (defn, definition_hash(path))

    return result
