"""Shared CLI context helpers: project-root resolution and engine access."""

from __future__ import annotations

from pathlib import Path

import click

from turnstile_core.runtime.engine import Engine

TURNSTILE_REPO_URL = "https://github.com/PaperArmada/turnstile.git"


def get_engine(project_root: str | None = None) -> Engine:
    """Build an Engine rooted at the given directory (default: CWD)."""
    root = Path(project_root) if project_root else Path.cwd()
    return Engine(root)


def project_root(ctx: click.Context) -> Path:
    """Resolve the project root from the CLI context (default: CWD)."""
    project = ctx.obj.get("project") if ctx.obj else None
    return Path(project) if project else Path.cwd()
