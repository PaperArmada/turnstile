"""Registry resolution: locate and load external process sources.

Supports three source types:
- Local paths (absolute or relative to project root)
- Git repositories (cloned into a local cache)
- Python packages (discovered via entry points or importlib)
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import logging
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any

from turnstile_core.errors import RegistryResolutionError
from turnstile_core.models import RegistryExtend

logger = logging.getLogger(__name__)


class SourceType(str, Enum):
    local = "local"
    git = "git"
    python = "python"


def parse_source(source: str) -> tuple[SourceType, str, str]:
    """Parse a source string into (type, location, ref).

    Examples:
        "./shared"              -> (local, "./shared", "")
        "/absolute/path"        -> (local, "/absolute/path", "")
        "git+https://...#v1.0"  -> (git, "https://...", "v1.0")
        "pypi:acme-processes"   -> (python, "acme-processes", "")
        "acme-processes"        -> (python, "acme-processes", "")
    """
    if source.startswith(("./", "../", "/")):
        return SourceType.local, source, ""

    if source.startswith("git+"):
        url = source[4:]
        ref = ""
        if "#" in url:
            url, ref = url.rsplit("#", 1)
        return SourceType.git, url, ref

    if source.startswith("pypi:"):
        return SourceType.python, source[5:], ""

    # Bare name -> Python package
    return SourceType.python, source, ""


def resolve_source(
    extend: RegistryExtend,
    project_root: Path,
    cache_dir: Path,
) -> tuple[Path, SourceType]:
    """Resolve an extends entry to a local directory containing YAML files.

    Returns (directory_path, source_type).
    """
    source_type, location, ref = parse_source(extend.source)

    if source_type == SourceType.local:
        return _resolve_local(location, project_root), source_type
    elif source_type == SourceType.git:
        return _resolve_git(location, ref, cache_dir), source_type
    elif source_type == SourceType.python:
        return _resolve_python(location), source_type
    else:
        raise RegistryResolutionError(f"Unknown source type: {source_type}")


def _resolve_local(location: str, project_root: Path) -> Path:
    """Resolve a local path source."""
    path = Path(location)
    if not path.is_absolute():
        path = project_root / path

    # Look for a processes/ subdirectory first, fall back to the path itself
    processes_dir = path / "processes"
    if processes_dir.is_dir():
        return processes_dir

    if path.is_dir():
        return path

    raise RegistryResolutionError(
        f"Local source path does not exist: {path}"
    )


def _resolve_git(url: str, ref: str, cache_dir: Path) -> Path:
    """Clone or update a git repository into the cache."""
    # Create a stable directory name from the URL
    safe_name = url.replace("://", "_").replace("/", "_").replace(".", "_")
    repo_dir = cache_dir / "git" / safe_name

    try:
        if repo_dir.exists():
            _git_fetch_and_checkout(repo_dir, ref)
        else:
            _git_clone(url, ref, repo_dir)
    except subprocess.CalledProcessError as e:
        raise RegistryResolutionError(
            f"Git operation failed for {url}: {e.stderr or e.stdout or str(e)}"
        ) from e
    except FileNotFoundError:
        raise RegistryResolutionError(
            "git is not installed or not in PATH"
        )

    # Look for processes/ subdirectory
    processes_dir = repo_dir / "processes"
    if processes_dir.is_dir():
        return processes_dir

    return repo_dir


def _git_clone(url: str, ref: str, target: Path) -> None:
    """Shallow-clone a git repository."""
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd.extend(["--branch", ref])
    cmd.extend([url, str(target)])

    logger.info(f"Cloning {url} (ref={ref or 'default'}) into {target}")
    subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
    )


def _git_fetch_and_checkout(repo_dir: Path, ref: str) -> None:
    """Fetch latest and checkout the specified ref."""
    if not ref:
        return  # No ref specified, keep current state

    logger.info(f"Fetching updates in {repo_dir} (ref={ref})")
    subprocess.run(
        ["git", "fetch", "--depth", "1", "origin", ref],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "FETCH_HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )


def _resolve_python(package_name: str) -> Path:
    """Resolve a Python package containing process definitions.

    Looks for the package's processes/ directory using:
    1. Entry points in the 'turnstile.processes' group
    2. Direct module import and resource lookup
    """
    # Try entry points first
    try:
        eps = importlib.metadata.entry_points(group="turnstile.processes")
        for ep in eps:
            if ep.name == "processes" and ep.value.startswith(
                package_name.replace("-", "_")
            ):
                module = ep.load()
                module_dir = Path(module.__file__).parent
                processes_dir = module_dir / "processes"
                if processes_dir.is_dir():
                    return processes_dir
                return module_dir
    except Exception:
        pass

    # Try direct import
    module_name = package_name.replace("-", "_")
    try:
        module = __import__(module_name)
        module_dir = Path(module.__file__).parent
        processes_dir = module_dir / "processes"
        if processes_dir.is_dir():
            return processes_dir
        return module_dir
    except ImportError:
        pass

    raise RegistryResolutionError(
        f"Python package '{package_name}' not found. "
        f"Install it with: uv add {package_name}"
    )


def load_extended_definitions(
    extends: list[RegistryExtend],
    project_root: Path,
    cache_dir: Path,
) -> dict[str, tuple[Path, SourceType, list[str]]]:
    """Resolve all extends entries and return paths to process directories.

    Returns a dict mapping source identifier to
    (resolved_dir, source_type, process_filter).

    The process_filter is the list of process names to import from that
    source (empty means import all).
    """
    results: dict[str, tuple[Path, SourceType, list[str]]] = {}

    for extend in extends:
        try:
            resolved_dir, source_type = resolve_source(
                extend, project_root, cache_dir
            )
            results[extend.source] = (
                resolved_dir,
                source_type,
                extend.processes,
            )
        except RegistryResolutionError:
            logger.warning(
                f"Failed to resolve source '{extend.source}', skipping"
            )
            raise

    return results
