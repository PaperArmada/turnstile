#!/usr/bin/env python3
"""Generate turnstile_cli/starters.py from the repo's .processes/ pack.

The CLI ships the curated starter pack embedded as Python data (mirroring
principles.py) so that `turnstile init` installs a working set of process
definitions offline, pinned to the installed package version — no git clone
at init time. Run this whenever the .processes/ pack changes:

    uv run python scripts/gen_starters.py

The registry is embedded with its enforcement value replaced by the sentinel
__ENFORCEMENT__, which `init` substitutes with the chosen mode.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROCESSES_DIR = REPO_ROOT / ".processes"
OUT_PATH = (
    REPO_ROOT
    / "packages"
    / "turnstile-cli"
    / "src"
    / "turnstile_cli"
    / "starters.py"
)

HEADER = '''\
"""Curated starter process pack, shipped by `turnstile init`.

GENERATED FILE — do not edit by hand. Regenerate from the repo's .processes/
pack with:

    uv run python scripts/gen_starters.py

STARTERS maps each definition's filename to its YAML content. STARTER_REGISTRY
is the registry.yaml content with the enforcement value replaced by the
sentinel __ENFORCEMENT__, which `init` substitutes with the selected mode.
"""

'''


def main() -> None:
    yaml_files = sorted(
        p for p in PROCESSES_DIR.glob("*.yaml") if p.name != "registry.yaml"
    )
    if not yaml_files:
        raise SystemExit(f"No starter definitions found in {PROCESSES_DIR}")

    registry_path = PROCESSES_DIR / "registry.yaml"
    if not registry_path.exists():
        raise SystemExit(f"No registry.yaml in {PROCESSES_DIR}")

    registry_text = registry_path.read_text()
    # Replace the enforcement value with a substitutable sentinel.
    registry_text, n = re.subn(
        r"(?m)^(\s*enforcement:\s*)\S+\s*$",
        r"\1__ENFORCEMENT__\n",
        registry_text,
    )
    if n != 1:
        raise SystemExit(
            f"Expected exactly one enforcement line in registry.yaml, found {n}"
        )

    lines = [HEADER, "STARTERS: dict[str, str] = {"]
    for path in yaml_files:
        content = path.read_text()
        lines.append(f"    {json.dumps(path.name)}: {json.dumps(content)},")
    lines.append("}")
    lines.append("")
    lines.append(f"STARTER_REGISTRY: str = {json.dumps(registry_text)}")
    lines.append("")

    OUT_PATH.write_text("\n".join(lines))
    print(f"Wrote {OUT_PATH.relative_to(REPO_ROOT)} "
          f"({len(yaml_files)} definitions)")


if __name__ == "__main__":
    main()
