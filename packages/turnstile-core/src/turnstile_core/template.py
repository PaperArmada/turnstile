"""Scaffolding for shared process packages."""

from __future__ import annotations

from pathlib import Path
from typing import Any


_PYPROJECT_TEMPLATE = """\
[project]
name = "{name}"
version = "1.0.0"
description = "Shared process definitions"
requires-python = ">=3.12"

[project.entry-points."turnstile.processes"]
processes = "{module_name}"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""

_INIT_TEMPLATE = """\
\"""Shared process definitions for {name}.\"""
"""

_SAMPLE_PROCESS_TEMPLATE = """\
name: {process_name}
description: "TODO: describe this process"
version: "1.0.0"

parameters:
  - name: task_name
    description: "Name of the task"
    required: true

states:
  - id: start
    type: initial
    transitions: [working]

  - id: working
    description: "Do the work"
    transitions: [done]

  - id: done
    type: terminal
"""

_README_TEMPLATE = """\
# {name}

Shared process definitions for use with [Turnstile](https://github.com/PaperArmada/turnstile).

## Installation

```bash
uv add {name}
```

## Usage

Add to your project's `.processes/registry.yaml`:

```yaml
version: "1.0"
extends:
  - source: "{name}"
    processes: [{processes_list}]
```

## Processes

{processes_section}

## Customization

Override any process by creating a file in `.processes/overrides/`:

```yaml
extends: "{first_process}"
overrides:
  patch_states:
    - id: working
      transitions: [review, working]
```
"""


def scaffold_package(
    name: str,
    output_dir: Path,
    processes: list[str] | None = None,
) -> dict[str, Path]:
    """Generate a starter shared process package.

    Returns a dict of created file paths.
    """
    if not processes:
        processes = ["example"]

    module_name = name.replace("-", "_")

    # Create directory structure
    src_dir = output_dir / "src" / module_name
    proc_dir = src_dir / "processes"
    proc_dir.mkdir(parents=True, exist_ok=True)

    created: dict[str, Path] = {}

    # pyproject.toml
    pyproject = output_dir / "pyproject.toml"
    pyproject.write_text(
        _PYPROJECT_TEMPLATE.format(name=name, module_name=module_name)
    )
    created["pyproject.toml"] = pyproject

    # __init__.py
    init = src_dir / "__init__.py"
    init.write_text(_INIT_TEMPLATE.format(name=name))
    created["__init__.py"] = init

    # Process definitions
    for proc_name in processes:
        proc_file = proc_dir / f"{proc_name}.yaml"
        proc_file.write_text(
            _SAMPLE_PROCESS_TEMPLATE.format(process_name=proc_name)
        )
        created[f"processes/{proc_name}.yaml"] = proc_file

    # README
    processes_list = ", ".join(processes)
    processes_section = "\n".join(
        f"- **{p}**: TODO describe" for p in processes
    )
    readme = output_dir / "README.md"
    readme.write_text(
        _README_TEMPLATE.format(
            name=name,
            processes_list=processes_list,
            processes_section=processes_section,
            first_process=processes[0],
        )
    )
    created["README.md"] = readme

    return created
