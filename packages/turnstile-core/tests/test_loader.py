"""Tests for turnstile_core.loader."""

from pathlib import Path

import pytest

from turnstile_core.errors import DefinitionError, ProcessNotFoundError
from turnstile_core.loader import (
    definition_hash,
    discover_definitions,
    load_definition,
    load_registry,
)

FIXTURES = Path(__file__).parent / "fixtures"


class TestDefinitionHash:
    def test_deterministic(self):
        path = FIXTURES / "feature-deploy.yaml"
        h1 = definition_hash(path)
        h2 = definition_hash(path)
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_different_for_different_files(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("hello")
        f2.write_text("world")
        assert definition_hash(f1) != definition_hash(f2)


class TestLoadDefinition:
    def test_valid_file(self):
        defn = load_definition(FIXTURES / "feature-deploy.yaml")
        assert defn.name == "feature-deploy"

    def test_missing_file(self):
        with pytest.raises(DefinitionError, match="not found"):
            load_definition(Path("/nonexistent/file.yaml"))

    def test_invalid_yaml(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(": : : not valid yaml [")
        with pytest.raises(DefinitionError, match="Invalid YAML"):
            load_definition(bad)

    def test_not_a_mapping(self, tmp_path):
        bad = tmp_path / "list.yaml"
        bad.write_text("- item1\n- item2\n")
        with pytest.raises(DefinitionError, match="Expected a YAML mapping"):
            load_definition(bad)

    def test_invalid_schema(self, tmp_path):
        bad = tmp_path / "bad_schema.yaml"
        bad.write_text("name: test\nstates: []\n")
        with pytest.raises(DefinitionError, match="Invalid process definition"):
            load_definition(bad)


class TestLoadRegistry:
    def test_missing_returns_defaults(self, tmp_path):
        reg = load_registry(tmp_path)
        assert reg.version == "1.0"
        assert reg.local == []

    def test_valid_registry(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        (proc_dir / "registry.yaml").write_text(
            "version: '1.0'\nlocal:\n  - my-process\n"
        )
        reg = load_registry(tmp_path)
        assert reg.local == ["my-process"]

    def test_empty_file_returns_defaults(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        (proc_dir / "registry.yaml").write_text("")
        reg = load_registry(tmp_path)
        assert reg.version == "1.0"


class TestDiscoverDefinitions:
    def test_no_processes_dir(self, tmp_path):
        result = discover_definitions(tmp_path)
        assert result == {}

    def test_auto_discover(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        # Copy our fixture
        import shutil

        shutil.copy(FIXTURES / "feature-deploy.yaml", proc_dir / "feature-deploy.yaml")
        result = discover_definitions(tmp_path)
        assert "feature-deploy" in result
        defn, h = result["feature-deploy"]
        assert defn.name == "feature-deploy"
        assert h.startswith("sha256:")

    def test_registry_local_list(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        (proc_dir / "registry.yaml").write_text(
            "version: '1.0'\nlocal:\n  - feature-deploy\n"
        )
        import shutil

        shutil.copy(FIXTURES / "feature-deploy.yaml", proc_dir / "feature-deploy.yaml")
        result = discover_definitions(tmp_path)
        assert "feature-deploy" in result

    def test_registry_lists_missing_file(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        (proc_dir / "registry.yaml").write_text(
            "version: '1.0'\nlocal:\n  - nonexistent\n"
        )
        with pytest.raises(ProcessNotFoundError, match="nonexistent"):
            discover_definitions(tmp_path)

    def test_auto_discover_skips_registry(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        (proc_dir / "registry.yaml").write_text("version: '1.0'\n")
        import shutil

        shutil.copy(FIXTURES / "feature-deploy.yaml", proc_dir / "feature-deploy.yaml")
        result = discover_definitions(tmp_path)
        # Should find feature-deploy but not try to parse registry.yaml as a process
        assert "feature-deploy" in result
        assert len(result) == 1
