#!/usr/bin/env python3
"""Generate turnstile_cli/starters.py from the repo's .processes/ pack.

The CLI ships the curated starter pack embedded as Python data (mirroring
principles.py) so that `turnstile init` installs a working set of process
definitions offline, pinned to the installed package version — no git clone
at init time. Run this whenever the .processes/ pack changes:

    uv run python scripts/gen_starters.py

The registry is embedded with its enforcement value replaced by the sentinel
__ENFORCEMENT__, which `init` substitutes with the chosen mode.

It also writes starter_history.py: the content hash of every starter file
shipped in each release tag (v*) present in this clone, so `turnstile update`
can recognise an unedited copy installed by any earlier release. Hashes
already in starter_history.py are kept, so a clone missing tags cannot shrink
it; fetch tags before regenerating to add the latest release.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
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

HISTORY_PATH = OUT_PATH.with_name("starter_history.py")
STARTERS_REL = "packages/turnstile-cli/src/turnstile_cli/starters.py"

HISTORY_HEADER = '''\
"""Content hashes of the starter definitions shipped in each release.

GENERATED FILE — do not edit by hand. Regenerate with:

    uv run python scripts/gen_starters.py

RELEASED_STARTER_HASHES maps a starter filename to the sha256 of every
version of it shipped in a release tag (see turnstile_cli.upgrade.content_hash
for the normalisation). `turnstile update` treats a local copy whose hash is
listed here as unedited, so installs that predate the starter manifest are
still upgraded safely.
"""

'''

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


def content_hash(text: str) -> str:
    """Must match turnstile_cli.upgrade.content_hash."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest()


def _released_starters(tag: str) -> dict[str, str]:
    """STARTERS as shipped at a tag, or {} if the tag has no bundle."""
    shown = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{tag}:{STARTERS_REL}"],
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        return {}
    for node in ast.parse(shown.stdout).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) else None
        )
        if isinstance(target, ast.Name) and target.id == "STARTERS":
            return ast.literal_eval(node.value)
    raise SystemExit(f"{tag}: no STARTERS assignment in {STARTERS_REL}")


def _existing_history() -> dict[str, list[str]]:
    if not HISTORY_PATH.exists():
        return {}
    for node in ast.parse(HISTORY_PATH.read_text()).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "RELEASED_STARTER_HASHES"
        ):
            return ast.literal_eval(node.value)
    return {}


def write_history() -> None:
    tags = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "tag", "-l", "v*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    # Start from the checked-in history: a clone missing some tags must not
    # drop hashes a release already shipped (that would make unedited
    # copies read as edited).
    hashes: dict[str, set[str]] = {
        filename: set(known) for filename, known in _existing_history().items()
    }
    if not tags:
        print("Warning: no release tags found; keeping the existing history")
    for tag in tags:
        for filename, content in _released_starters(tag).items():
            hashes.setdefault(filename, set()).add(content_hash(content))
    lines = [HISTORY_HEADER, "RELEASED_STARTER_HASHES: dict[str, list[str]] = {"]
    for filename in sorted(hashes):
        lines.append(f"    {json.dumps(filename)}: [")
        lines.extend(f"        {json.dumps(h)}," for h in sorted(hashes[filename]))
        lines.append("    ],")
    lines.append("}")
    lines.append("")
    HISTORY_PATH.write_text("\n".join(lines))
    print(f"Wrote {HISTORY_PATH.relative_to(REPO_ROOT)} "
          f"({len(tags)} tags, {sum(map(len, hashes.values()))} hashes)")


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
    write_history()


if __name__ == "__main__":
    main()
