"""Load and validate process definitions from YAML files."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from turnstile_core.errors import DefinitionError, InheritanceError, ProcessNotFoundError
from turnstile_core.definition.inheritance import resolve_inheritance
from turnstile_core.definition.model import ProcessDefinition, ProcessOverride, RegistryConfig
from turnstile_core.definition.registry import SourceType, load_extended_definitions

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


def load_override(path: Path) -> ProcessOverride:
    """Load a process override YAML file.

    Override files have an 'extends' key at the top level and an
    'overrides' block instead of a 'states' list.
    """
    if not path.exists():
        raise DefinitionError(f"Override file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise DefinitionError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise DefinitionError(f"Expected a YAML mapping in {path}")

    try:
        return ProcessOverride(**raw)
    except Exception as e:
        raise DefinitionError(f"Invalid override in {path}: {e}") from e


def is_override_file(path: Path) -> bool:
    """Check if a YAML file is an override.

    Overrides have both an 'extends' key and an 'overrides' block.
    Requiring both avoids misclassifying a full definition that happens
    to carry a stray 'overrides' key.
    """
    try:
        raw = yaml.safe_load(path.read_text())
        return isinstance(raw, dict) and "overrides" in raw and "extends" in raw
    except Exception:
        return False


def _resolve_overrides(
    override_paths: list[Path],
    available: dict[str, DiscoveredDefinition],
    strict_paths: frozenset[Path] = frozenset(),
    lenient_paths: frozenset[Path] = frozenset(),
) -> dict[str, DiscoveredDefinition]:
    """Resolve override files against available definitions.

    Override files extend parent definitions that must already be
    loaded (from extended sources or local definitions). They may live
    in .processes/overrides/ or as top-level .processes/*.yaml files.

    Error strictness depends on how the file was found:
    - strict_paths (explicitly listed in registry.local): any load or
      resolution failure raises.
    - lenient_paths (auto-discovered top-level files): a missing parent
      is logged and the file skipped, so one stray file cannot take
      down discovery (fail open).
    - overrides/ directory files (neither set): invalid files are
      skipped with a warning; a missing parent raises.
    """
    results: dict[str, DiscoveredDefinition] = {}

    for path in override_paths:
        try:
            override = load_override(path)
        except DefinitionError as e:
            if path in strict_paths:
                raise
            logger.warning(f"Skipping invalid override {path}: {e}")
            continue

        # Find the parent definition by matching the extends reference.
        # The extends field can be a source/name like
        # "@org/package/release" or just a name like "release".
        parent_name = override.extends.rsplit("/", 1)[-1]
        parent_disc = available.get(parent_name)
        if parent_disc is None:
            if path in lenient_paths:
                logger.warning(
                    f"Skipping override {path}: no parent definition "
                    f"named '{parent_name}' was found"
                )
                continue
            raise InheritanceError(
                f"Override in {path} extends '{override.extends}' "
                f"but no parent definition named '{parent_name}' was found"
            )

        try:
            merged = resolve_inheritance(override, parent_disc.definition)
        except InheritanceError:
            raise
        except Exception as e:
            raise InheritanceError(
                f"Failed to resolve override {path}: {e}"
            ) from e

        # An override may patch its own parent in place (no distinct
        # name), but colliding with an unrelated local definition is a
        # configuration error, not a silent shadow.
        existing = available.get(merged.name)
        if (
            existing is not None
            and existing.source == "local"
            and merged.name != parent_name
        ):
            raise InheritanceError(
                f"Override {path} resolves to name '{merged.name}', "
                f"which collides with the local definition at "
                f"{existing.source_path}. Rename the override or "
                f"remove the local definition."
            )

        if merged.name in results:
            logger.warning(
                f"Multiple override files resolve to '{merged.name}'; "
                f"{path} takes precedence"
            )
        results[merged.name] = DiscoveredDefinition(
            definition=merged,
            file_hash=definition_hash(path),
            source=f"override:{parent_disc.source}",
            source_path=str(path),
        )

    return results


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


def _discover_all(project_root: Path) -> dict[str, DiscoveredDefinition]:
    """Internal: discover all definitions from all sources.

    Loading order:
    1. Extended sources from registry.yaml (lowest priority)
    2. Local definitions from .processes/*.yaml or registry.local
       (override extended sources)
    3. Override files (.processes/overrides/ plus override-shaped local
       files), resolved against everything above. An override may patch
       its own parent in place; colliding with an unrelated local
       definition raises.
    """
    processes_dir = project_root / ".processes"
    if not processes_dir.exists():
        return {}

    registry = load_registry(project_root)
    all_discovered: dict[str, DiscoveredDefinition] = {}

    # 1. Load from extended sources (lowest priority)
    if registry.extends:
        cache_dir = project_root / registry.settings.state_dir / ".cache"
        extended = load_extended_definitions(
            registry.extends, project_root, cache_dir
        )
        for source, (resolved_dir, source_type, process_filter) in extended.items():
            label = _source_label(source, source_type)
            defs = _load_from_directory(resolved_dir, process_filter, label)
            all_discovered.update(defs)

    # 2. Load local definitions. Override-shaped files (with an
    #    'overrides' block) are deferred to step 3 so they can extend
    #    local parents as well as extended sources.
    local_override_paths: list[Path] = []
    if registry.local:
        for name in registry.local:
            path = processes_dir / f"{name}.yaml"
            if not path.exists():
                raise ProcessNotFoundError(
                    f"Local process '{name}' listed in registry but "
                    f"file not found: {path}"
                )
            if is_override_file(path):
                local_override_paths.append(path)
                continue
            defn = load_definition(path)
            if defn.name in all_discovered:
                logger.info(
                    f"Local definition '{defn.name}' overrides "
                    f"source '{all_discovered[defn.name].source}'"
                )
            all_discovered[defn.name] = DiscoveredDefinition(
                definition=defn,
                file_hash=definition_hash(path),
                source="local",
                source_path=str(path),
            )
    else:
        # Auto-discover .yaml files (excluding registry and overrides dir)
        for path in sorted(processes_dir.glob("*.yaml")):
            if path.name == "registry.yaml":
                continue
            if is_override_file(path):
                local_override_paths.append(path)
                continue
            try:
                defn = load_definition(path)
            except DefinitionError as e:
                # Fail open: one stray or malformed YAML (a policy file,
                # a work in progress) must not brick discovery of every
                # other definition. Skip it, loudly.
                logger.warning(
                    "Skipping %s during discovery: %s", path.name, e
                )
                continue
            if defn.name in all_discovered:
                logger.info(
                    f"Local definition '{defn.name}' overrides "
                    f"source '{all_discovered[defn.name].source}'"
                )
            all_discovered[defn.name] = DiscoveredDefinition(
                definition=defn,
                file_hash=definition_hash(path),
                source="local",
                source_path=str(path),
            )

    # 3. Resolve override files against extended + local definitions.
    #    Registry-listed files are strict (failures raise); auto-
    #    discovered top-level files are lenient (skipped with a warning
    #    if their parent is missing).
    override_paths: list[Path] = []
    overrides_dir = processes_dir / "overrides"
    if overrides_dir.is_dir():
        override_paths.extend(sorted(overrides_dir.glob("*.yaml")))
    override_paths.extend(local_override_paths)
    local_override_set = frozenset(local_override_paths)
    all_discovered.update(
        _resolve_overrides(
            override_paths,
            all_discovered,
            strict_paths=local_override_set if registry.local else frozenset(),
            lenient_paths=frozenset() if registry.local else local_override_set,
        )
    )

    return all_discovered


def discover_definitions(
    project_root: Path,
) -> dict[str, tuple[ProcessDefinition, str]]:
    """Discover all process definitions for a project.

    Returns a dict mapping process name to (definition, file_hash).
    """
    all_discovered = _discover_all(project_root)
    return {
        name: (disc.definition, disc.file_hash)
        for name, disc in all_discovered.items()
    }


def discover_definitions_full(
    project_root: Path,
) -> dict[str, DiscoveredDefinition]:
    """Discover all process definitions with full metadata.

    Like discover_definitions() but returns DiscoveredDefinition objects
    instead of (definition, hash) tuples.
    """
    return _discover_all(project_root)
