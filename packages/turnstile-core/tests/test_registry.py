"""Tests for turnstile_core.definition.registry (source resolution)."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from turnstile_core.errors import RegistryResolutionError
from turnstile_core.definition.model import RegistryExtend
from turnstile_core.definition.registry import (
    SourceType,
    parse_source,
    resolve_source,
    load_extended_definitions,
    _resolve_local,
    _resolve_git,
    _resolve_python,
)


# ---------------------------------------------------------------------------
# parse_source
# ---------------------------------------------------------------------------


class TestParseSource:
    def test_relative_path_dot(self):
        stype, loc, ref = parse_source("./shared")
        assert stype == SourceType.local
        assert loc == "./shared"
        assert ref == ""

    def test_relative_path_dotdot(self):
        stype, loc, ref = parse_source("../other/processes")
        assert stype == SourceType.local
        assert loc == "../other/processes"

    def test_absolute_path(self):
        stype, loc, ref = parse_source("/opt/processes")
        assert stype == SourceType.local
        assert loc == "/opt/processes"

    def test_git_url_with_ref(self):
        stype, loc, ref = parse_source("git+https://github.com/org/lib.git#v1.0")
        assert stype == SourceType.git
        assert loc == "https://github.com/org/lib.git"
        assert ref == "v1.0"

    def test_git_url_no_ref(self):
        stype, loc, ref = parse_source("git+https://github.com/org/lib.git")
        assert stype == SourceType.git
        assert loc == "https://github.com/org/lib.git"
        assert ref == ""

    def test_git_ssh(self):
        stype, loc, ref = parse_source("git+git@github.com:org/lib.git#main")
        assert stype == SourceType.git
        assert loc == "git@github.com:org/lib.git"
        assert ref == "main"

    def test_pypi_prefix(self):
        stype, loc, ref = parse_source("pypi:acme-processes")
        assert stype == SourceType.python
        assert loc == "acme-processes"
        assert ref == ""

    def test_bare_name(self):
        stype, loc, ref = parse_source("acme-processes")
        assert stype == SourceType.python
        assert loc == "acme-processes"


# ---------------------------------------------------------------------------
# Local resolution
# ---------------------------------------------------------------------------


class TestResolveLocal:
    def test_direct_directory(self, tmp_path):
        process_dir = tmp_path / "myprocesses"
        process_dir.mkdir()
        (process_dir / "foo.yaml").write_text("name: foo\nstates: []\n")

        result = _resolve_local(str(process_dir), tmp_path)
        assert result == process_dir

    def test_processes_subdirectory(self, tmp_path):
        pkg_dir = tmp_path / "shared-pkg"
        pkg_dir.mkdir()
        proc_dir = pkg_dir / "processes"
        proc_dir.mkdir()
        (proc_dir / "foo.yaml").write_text("name: foo\n")

        result = _resolve_local(str(pkg_dir), tmp_path)
        assert result == proc_dir

    def test_relative_path(self, tmp_path):
        process_dir = tmp_path / "shared"
        process_dir.mkdir()

        result = _resolve_local("./shared", tmp_path)
        assert result == process_dir

    def test_nonexistent_raises(self, tmp_path):
        with pytest.raises(RegistryResolutionError, match="does not exist"):
            _resolve_local("./nonexistent", tmp_path)


# ---------------------------------------------------------------------------
# Git resolution (mocked)
# ---------------------------------------------------------------------------


class TestResolveGit:
    @patch("turnstile_core.definition.registry.subprocess.run")
    def test_clone_new_repo(self, mock_run, tmp_path):
        cache_dir = tmp_path / "cache"
        url = "https://github.com/org/lib.git"

        result = _resolve_git(url, "v1.0", cache_dir)

        # Should have called git clone
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "git"
        assert args[1] == "clone"
        assert "--branch" in args
        assert "v1.0" in args

    @patch("turnstile_core.definition.registry.subprocess.run")
    def test_fetch_existing_repo(self, mock_run, tmp_path):
        cache_dir = tmp_path / "cache"
        url = "https://github.com/org/lib.git"

        # Pre-create the repo directory
        safe_name = url.replace("://", "_").replace("/", "_").replace(".", "_")
        repo_dir = cache_dir / "git" / safe_name
        repo_dir.mkdir(parents=True)

        result = _resolve_git(url, "v2.0", cache_dir)

        # Should have called git fetch + git checkout
        assert mock_run.call_count == 2

    @patch("turnstile_core.definition.registry.subprocess.run")
    def test_clone_failure_raises(self, mock_run, tmp_path):
        mock_run.side_effect = subprocess.CalledProcessError(
            128, "git", stderr="fatal: repository not found"
        )
        cache_dir = tmp_path / "cache"

        with pytest.raises(RegistryResolutionError, match="Git operation failed"):
            _resolve_git("https://bad.url/repo.git", "main", cache_dir)

    def test_git_not_installed(self, tmp_path):
        cache_dir = tmp_path / "cache"
        with patch(
            "turnstile_core.definition.registry.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            with pytest.raises(RegistryResolutionError, match="git is not installed"):
                _resolve_git("https://any.url/repo.git", "main", cache_dir)

    @patch("turnstile_core.definition.registry.subprocess.run")
    def test_processes_subdir_preferred(self, mock_run, tmp_path):
        cache_dir = tmp_path / "cache"
        url = "https://github.com/org/lib.git"

        safe_name = url.replace("://", "_").replace("/", "_").replace(".", "_")
        repo_dir = cache_dir / "git" / safe_name
        proc_dir = repo_dir / "processes"
        proc_dir.mkdir(parents=True)

        result = _resolve_git(url, "", cache_dir)
        assert result == proc_dir


# ---------------------------------------------------------------------------
# Python package resolution (mocked)
# ---------------------------------------------------------------------------


class TestResolvePython:
    def test_import_with_processes_dir(self, tmp_path):
        # Create a fake package
        pkg_dir = tmp_path / "fake_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        proc_dir = pkg_dir / "processes"
        proc_dir.mkdir()
        (proc_dir / "release.yaml").write_text("name: release\n")

        fake_module = MagicMock()
        fake_module.__file__ = str(pkg_dir / "__init__.py")

        with patch("turnstile_core.definition.registry.importlib.metadata.entry_points", return_value=[]):
            with patch("builtins.__import__", return_value=fake_module):
                result = _resolve_python("fake-pkg")
                assert result == proc_dir

    def test_package_not_found(self):
        with patch("turnstile_core.definition.registry.importlib.metadata.entry_points", return_value=[]):
            with patch("builtins.__import__", side_effect=ImportError):
                with pytest.raises(RegistryResolutionError, match="not found"):
                    _resolve_python("nonexistent-package")


# ---------------------------------------------------------------------------
# Full resolution flow
# ---------------------------------------------------------------------------


class TestResolveSource:
    def test_local_source(self, tmp_path):
        process_dir = tmp_path / "shared"
        process_dir.mkdir()
        extend = RegistryExtend(source=str(process_dir), processes=[])
        result_path, result_type = resolve_source(extend, tmp_path, tmp_path / "cache")
        assert result_type == SourceType.local

    @patch("turnstile_core.definition.registry._resolve_git")
    def test_git_source(self, mock_git, tmp_path):
        mock_git.return_value = tmp_path
        extend = RegistryExtend(
            source="git+https://github.com/org/lib.git#v1.0",
            processes=["release"],
        )
        result_path, result_type = resolve_source(extend, tmp_path, tmp_path / "cache")
        assert result_type == SourceType.git
        mock_git.assert_called_once_with(
            "https://github.com/org/lib.git", "v1.0", tmp_path / "cache"
        )


# ---------------------------------------------------------------------------
# load_extended_definitions
# ---------------------------------------------------------------------------


class TestLoadExtendedDefinitions:
    def _make_process_yaml(self, directory: Path, name: str) -> None:
        """Create a minimal valid process YAML."""
        data = {
            "name": name,
            "description": f"The {name} process",
            "version": "1.0.0",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (directory / f"{name}.yaml").write_text(yaml.dump(data))

    def test_local_source(self, tmp_path):
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        self._make_process_yaml(shared_dir, "release")
        self._make_process_yaml(shared_dir, "review")

        extends = [
            RegistryExtend(source=str(shared_dir), processes=["release"]),
        ]
        cache_dir = tmp_path / "cache"

        result = load_extended_definitions(extends, tmp_path, cache_dir)
        assert str(shared_dir) in result
        resolved_dir, source_type, proc_filter = result[str(shared_dir)]
        assert source_type == SourceType.local
        assert proc_filter == ["release"]

    def test_processes_subdir_layout(self, tmp_path):
        """Source with a processes/ subdirectory."""
        pkg_dir = tmp_path / "shared-pkg"
        proc_dir = pkg_dir / "processes"
        proc_dir.mkdir(parents=True)
        self._make_process_yaml(proc_dir, "deploy")

        extends = [RegistryExtend(source=str(pkg_dir), processes=[])]
        result = load_extended_definitions(extends, tmp_path, tmp_path / "cache")
        resolved_dir, _, _ = result[str(pkg_dir)]
        assert resolved_dir == proc_dir
