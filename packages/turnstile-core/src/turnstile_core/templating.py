"""Parameter templating: ``${var}`` substitution and placeholder detection.

Pure string transformations shared by the definition layer (parameter
maps), the kernel (child parameter resolution), and the runtime (gate
commands). No I/O.
"""

from __future__ import annotations

import re

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def substitute_params(command: str, parameters: dict[str, str]) -> str:
    """Replace ``${var_name}`` placeholders in a command string."""
    result = command
    for key, value in parameters.items():
        result = result.replace(f"${{{key}}}", str(value))
    return result


def unresolved_placeholders(value: str) -> list[str]:
    """Return the sorted, de-duplicated ``${...}`` names left in a string.

    A surviving placeholder after substitution means the template
    referenced a variable that was not available. Callers decide whether
    that is an error (dispatch parameter maps) or acceptable.
    """
    return sorted(set(_PLACEHOLDER_RE.findall(value)))
