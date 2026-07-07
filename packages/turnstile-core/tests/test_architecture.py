"""Architectural conformance tests.

Enforces the layering documented in docs/architecture.md:

    errors, templating -> definition -> instance -> kernel -> runtime -> ops

A module may import from its own layer or earlier layers, never later
ones. If this test fails, the fix is to move code, not to weaken the
test — the one-way import direction is what keeps decisions (kernel)
separable from side effects (runtime).
"""

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "turnstile_core"

# Layer index by top-level name inside turnstile_core.
LAYERS = {
    "errors": 0,
    "templating": 0,
    "definition": 1,
    "instance": 2,
    "kernel": 3,
    "runtime": 4,
    "ops": 5,
}


def _layer_of(module_parts: list[str]) -> int | None:
    """Layer index for a dotted turnstile_core module path, or None."""
    if not module_parts or module_parts[0] != "turnstile_core":
        return None
    if len(module_parts) == 1:
        # The package root __init__ re-exports across layers; importing
        # it from inside core would be a cycle anyway.
        return max(LAYERS.values())
    return LAYERS.get(module_parts[1])


def _imports_of(path: Path) -> list[str]:
    """All absolute dotted module names imported by a file."""
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return found


def _core_modules() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_layer_import_direction():
    violations = []
    for path in _core_modules():
        rel = path.relative_to(SRC)
        parts = ["turnstile_core"] + list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        own_layer = _layer_of(parts)
        if own_layer is None:
            continue
        for imported in _imports_of(path):
            target_layer = _layer_of(imported.split("."))
            if target_layer is None:
                continue
            if target_layer > own_layer:
                violations.append(
                    f"{rel}: imports {imported} "
                    f"(layer {target_layer} > own layer {own_layer})"
                )
    assert not violations, (
        "Layering violations (see docs/architecture.md):\n  "
        + "\n  ".join(violations)
    )


def test_kernel_is_pure():
    """The kernel must not import anything that can perform I/O."""
    forbidden = {
        "os", "io", "sys", "subprocess", "asyncio", "pathlib",
        "shutil", "socket", "json",  # json allowed elsewhere; kernel shouldn't serialize
        "turnstile_core.instance.store",
        "turnstile_core.runtime",
        "turnstile_core.ops",
    }
    kernel_dir = SRC / "kernel"
    violations = []
    for path in sorted(kernel_dir.rglob("*.py")):
        for imported in _imports_of(path):
            root = imported.split(".")[0]
            if imported in forbidden or root in forbidden:
                violations.append(f"{path.name}: imports {imported}")
    assert not violations, (
        "Kernel purity violations:\n  " + "\n  ".join(violations)
    )


def test_frontends_do_not_import_private_core_names():
    """CLI and MCP must consume the public surface only."""
    frontends = [
        SRC.parent.parent.parent / "turnstile-cli" / "src" / "turnstile_cli",
        SRC.parent.parent.parent / "turnstile-mcp" / "src" / "turnstile_mcp",
    ]
    violations = []
    for pkg in frontends:
        for path in sorted(pkg.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if not (node.module or "").startswith("turnstile_core"):
                    continue
                for alias in node.names:
                    if alias.name.startswith("_"):
                        violations.append(
                            f"{path.name}: from {node.module} "
                            f"import {alias.name}"
                        )
    assert not violations, (
        "Frontends importing private core names:\n  "
        + "\n  ".join(violations)
    )
