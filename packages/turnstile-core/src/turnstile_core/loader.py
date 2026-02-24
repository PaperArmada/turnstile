"""Load and validate process definitions from YAML files."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from turnstile_core.errors import DefinitionError, ProcessNotFoundError
from turnstile_core.models import ProcessDefinition, RegistryConfig
from turnstile_core.registry import SourceType, load_extended_definitions

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredDefinition:
    """A process definition with its metadata from discovery."""

    definition: ProcessDefinition
    file_hash: str
    source: str = "local"
    source_path: str = ""


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


def _source_label(source: str, source_type: SourceType) -> str:
    """Create a human-readable source label."""
    if source_type == SourceType.git:
        return f"git:{source}"
    elif source_type == SourceType.python:
        return f"python:{source}"
    return f"local:{source}"


def _load_from_directory(
    directory: Path,
    process_filter: list[str],
    source_label: str,
) -> dict[str, DiscoveredDefinition]:
    """Load process definitions from a directory, optionally filtered."""
    results: dict[str, DiscoveredDefinition] = {}

    for path in sorted(directory.glob("*.yaml")):
        if path.name == "registry.yaml":
            continue
        try:
            defn = load_definition(path)
        except DefinitionError as e:
            logger.warning(f"Skipping invalid definition {path}: {e}")
            continue

        # Apply filter: if process_filter is set, only load matching names
        if process_filter and defn.name not in process_filter:
            continue

        results[defn.name] = DiscoveredDefinition(
            definition=defn,
            file_hash=definition_hash(path),
            source=source_label,
            source_path=str(path),
        )

    return results


def discover_definitions(
    project_root: Path,
) -> dict[str, tuple[ProcessDefinition, str]]:
    """Discover all process definitions for a project.

    Returns a dict mapping process name to (definition, file_hash).

    Loads from three places in order:
    1. Extended sources from registry.yaml (lowest priority)
    2. Local definitions from .processes/*.yaml
    3. Explicitly listed local processes from registry.local

    Local definitions override extended ones with the same name.
    """
    processes_dir = project_root / ".processes"
    if not processes_dir.exists():
        return {}

    registry = load_registry(project_root)
    all_discovered: dict[str, DiscoveredDefinition] = {}

    # 1. Load from extended sources (lowest priority)
    if registry.extends:
        cache_dir = project_root / registry.settings.state_dir / ".cache"
        try:
            extended = load_extended_definitions(
                registry.extends, project_root, cache_dir
            )
            for source, (resolved_dir, source_type, process_filter) in extended.items():
                label = _source_label(source, source_type)
                defs = _load_from_directory(resolved_dir, process_filter, label)
                all_discovered.update(defs)
        except Exception as e:
            logger.warning(f"Failed to load extended definitions: {e}")
            raise

    # 2. Load local definitions (override extended ones)
    if registry.local:
        for name in registry.local:
            path = processes_dir / f"{name}.yaml"
            if not path.exists():
                raise ProcessNotFoundError(
                    f"Local process '{name}' listed in registry but "
                    f"file not found: {path}"
                )
            defn = load_definition(path)
            if defn.name in all_discovered:
                logger.info(
                    f"Local definition '{defn.name}' overrides "
                    f"extended source '{all_discovered[defn.name].source}'"
                )
            all_discovered[defn.name] = DiscoveredDefinition(
                definition=defn,
                file_hash=definition_hash(path),
                source="local",
                source_path=str(path),
            )
    else:
        # Auto-discover all .yaml files (excluding registry and overrides)
        for path in sorted(processes_dir.glob("*.yaml")):
            if path.name == "registry.yaml":
                continue
            defn = load_definition(path)
            if defn.name in all_discovered:
                logger.info(
                    f"Local definition '{defn.name}' overrides "
                    f"extended source '{all_discovered[defn.name].source}'"
                )
            all_discovered[defn.name] = DiscoveredDefinition(
                definition=defn,
                file_hash=definition_hash(path),
                source="local",
                source_path=str(path),
            )

    # Convert to the legacy tuple format for backward compatibility
    # Engine and other consumers use (definition, hash) tuples
    result: dict[str, tuple[ProcessDefinition, str]] = {}
    for name, disc in all_discovered.items():
        result[name] = (disc.definition, disc.file_hash)

    return result


def discover_definitions_full(
    project_root: Path,
) -> dict[str, DiscoveredDefinition]:
    """Discover all process definitions with full metadata.

    Like discover_definitions() but returns DiscoveredDefinition objects
    instead of (definition, hash) tuples.
    """
    processes_dir = project_root / ".processes"
    if not processes_dir.exists():
        return {}

    registry = load_registry(project_root)
    all_discovered: dict[str, DiscoveredDefinition] = {}

    if registry.extends:
        cache_dir = project_root / registry.settings.state_dir / ".cache"
        try:
            extended = load_extended_definitions(
                registry.extends, project_root, cache_dir
            )
            for source, (resolved_dir, source_type, process_filter) in extended.items():
                label = _source_label(source, source_type)
                defs = _load_from_directory(resolved_dir, process_filter, label)
                all_discovered.update(defs)
        except Exception as e:
            logger.warning(f"Failed to load extended definitions: {e}")
            raise

    if registry.local:
        for name in registry.local:
            path = processes_dir / f"{name}.yaml"
            if not path.exists():
                raise ProcessNotFoundError(
                    f"Local process '{name}' listed in registry but "
                    f"file not found: {path}"
                )
            defn = load_definition(path)
            all_discovered[defn.name] = DiscoveredDefinition(
                definition=defn,
                file_hash=definition_hash(path),
                source="local",
                source_path=str(path),
            )
    else:
        for path in sorted(processes_dir.glob("*.yaml")):
            if path.name == "registry.yaml":
                continue
            defn = load_definition(path)
            all_discovered[defn.name] = DiscoveredDefinition(
                definition=defn,
                file_hash=definition_hash(path),
                source="local",
                source_path=str(path),
            )

    return all_discovered
