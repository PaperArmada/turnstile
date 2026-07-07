"""JSON Schema generation from Pydantic models.

Exports JSON Schema for process definitions and registry config,
enabling editor autocomplete and validation for YAML files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from turnstile_core.definition.model import ProcessDefinition, RegistryConfig


def process_definition_schema() -> dict[str, Any]:
    """Generate JSON Schema for process definition YAML files."""
    schema = ProcessDefinition.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Turnstile Process Definition"
    schema["description"] = (
        "Schema for turnstile process definition YAML files "
        "(.processes/*.yaml)."
    )
    return schema


def registry_schema() -> dict[str, Any]:
    """Generate JSON Schema for registry.yaml."""
    schema = RegistryConfig.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Turnstile Registry Configuration"
    schema["description"] = (
        "Schema for the turnstile registry file "
        "(.processes/registry.yaml)."
    )
    return schema


def export_schemas(output_dir: Path) -> dict[str, Path]:
    """Export all schemas as JSON files to the given directory.

    Returns a dict mapping schema name to the written file path.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    schemas = {
        "process-definition": process_definition_schema(),
        "registry": registry_schema(),
    }

    written: dict[str, Path] = {}
    for name, schema in schemas.items():
        path = output_dir / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2) + "\n")
        written[name] = path

    return written
