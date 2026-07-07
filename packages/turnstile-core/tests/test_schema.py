"""Tests for turnstile_core.definition.schema (JSON Schema export)."""

import json

import pytest

from turnstile_core.definition.schema import (
    export_schemas,
    process_definition_schema,
    registry_schema,
)


class TestProcessDefinitionSchema:
    def test_has_json_schema_meta(self):
        schema = process_definition_schema()
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    def test_has_title(self):
        schema = process_definition_schema()
        assert "Turnstile" in schema["title"]

    def test_required_fields(self):
        schema = process_definition_schema()
        assert "name" in schema["properties"]
        assert "states" in schema["properties"]
        assert "name" in schema["required"]
        assert "states" in schema["required"]

    def test_states_is_array(self):
        schema = process_definition_schema()
        states_prop = schema["properties"]["states"]
        assert states_prop["type"] == "array"

    def test_serializable(self):
        schema = process_definition_schema()
        serialized = json.dumps(schema)
        roundtrip = json.loads(serialized)
        assert roundtrip["title"] == schema["title"]

    def test_definitions_include_key_types(self):
        schema = process_definition_schema()
        defs = schema.get("$defs", {})
        type_names = set(defs.keys())
        assert "ValidationRule" in type_names
        assert "ProcessState" in type_names
        assert "StateHooks" in type_names


class TestRegistrySchema:
    def test_has_json_schema_meta(self):
        schema = registry_schema()
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    def test_has_title(self):
        schema = registry_schema()
        assert "Registry" in schema["title"]

    def test_settings_referenced(self):
        schema = registry_schema()
        # settings should be in properties
        assert "settings" in schema["properties"]

    def test_serializable(self):
        schema = registry_schema()
        serialized = json.dumps(schema)
        roundtrip = json.loads(serialized)
        assert roundtrip == schema


class TestExportSchemas:
    def test_writes_files(self, tmp_path):
        written = export_schemas(tmp_path / "schemas")
        assert "process-definition" in written
        assert "registry" in written
        assert written["process-definition"].exists()
        assert written["registry"].exists()

    def test_file_contents_valid_json(self, tmp_path):
        written = export_schemas(tmp_path / "schemas")
        for path in written.values():
            data = json.loads(path.read_text())
            assert "$schema" in data

    def test_creates_directory(self, tmp_path):
        out = tmp_path / "nested" / "deep" / "schemas"
        assert not out.exists()
        export_schemas(out)
        assert out.exists()

    def test_overwrites_existing(self, tmp_path):
        out = tmp_path / "schemas"
        export_schemas(out)
        # Write again, should not error
        written = export_schemas(out)
        assert len(written) == 2
