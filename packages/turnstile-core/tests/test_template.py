"""Tests for turnstile_core.template (package scaffolding)."""

from pathlib import Path

from turnstile_core.template import scaffold_package


class TestScaffoldPackage:
    def test_creates_structure(self, tmp_path):
        out = tmp_path / "my-processes"
        created = scaffold_package("my-processes", out)

        assert (out / "pyproject.toml").exists()
        assert (out / "src" / "my_processes" / "__init__.py").exists()
        assert (out / "src" / "my_processes" / "processes" / "example.yaml").exists()
        assert (out / "README.md").exists()

    def test_custom_processes(self, tmp_path):
        out = tmp_path / "pkg"
        created = scaffold_package("acme-procs", out, ["release", "review"])

        assert (out / "src" / "acme_procs" / "processes" / "release.yaml").exists()
        assert (out / "src" / "acme_procs" / "processes" / "review.yaml").exists()
        assert not (out / "src" / "acme_procs" / "processes" / "example.yaml").exists()

    def test_pyproject_content(self, tmp_path):
        out = tmp_path / "pkg"
        scaffold_package("test-pkg", out)
        content = (out / "pyproject.toml").read_text()
        assert 'name = "test-pkg"' in content
        assert "turnstile.processes" in content

    def test_process_yaml_valid(self, tmp_path):
        out = tmp_path / "pkg"
        scaffold_package("test-pkg", out, ["deploy"])
        content = (out / "src" / "test_pkg" / "processes" / "deploy.yaml").read_text()
        assert "name: deploy" in content
        assert "type: initial" in content
        assert "type: terminal" in content

    def test_readme_references_processes(self, tmp_path):
        out = tmp_path / "pkg"
        scaffold_package("test-pkg", out, ["release", "review"])
        content = (out / "README.md").read_text()
        assert "release" in content
        assert "review" in content

    def test_returns_created_files(self, tmp_path):
        out = tmp_path / "pkg"
        created = scaffold_package("test-pkg", out)
        assert "pyproject.toml" in created
        assert "__init__.py" in created
        assert "README.md" in created
