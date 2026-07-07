"""The expect-expression grammar for validation gates.

An expect expression describes what a gate demands of a command's
output: ``not_empty``, ``contains("passed")``, ``exit_code(0)``, and so
on. This module owns the whole grammar — parsing an expression into a
structured form and compiling it into a checker function — so the
definition of "what counts as passing" lives in exactly one place.

Everything here is pure: no I/O, no engine coupling.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

_BARE_KEYWORDS = {"empty", "not_empty", "is_json", "is_json_object", "is_json_array"}
_FUNC_PATTERN = re.compile(
    r'^(equals|not_equals|contains|starts_with|ends_with|matches|matches_regex)\("(.*)"\)$'
)
_NUMERIC_PATTERN = re.compile(r"^(greater_than|less_than)\((\d+)\)$")
_EXIT_CODE_PATTERN = re.compile(r"^exit_code\((\d+)\)$")


def parse_expect(expr: str) -> dict[str, Any]:
    """Parse an expect expression string into a structured dict.

    Returns a dict with 'type' and relevant parameters. This is used
    for serialization/inspection; ``build_checker`` compiles the same
    grammar into an executable check.
    """
    expr = expr.strip()

    if expr in _BARE_KEYWORDS:
        return {"type": expr}

    m = _FUNC_PATTERN.match(expr)
    if m:
        return {"type": m.group(1), "value": m.group(2)}

    m = _NUMERIC_PATTERN.match(expr)
    if m:
        return {"type": m.group(1), "value": int(m.group(2))}

    m = _EXIT_CODE_PATTERN.match(expr)
    if m:
        return {"type": "exit_code", "value": int(m.group(1))}

    raise ValueError(f"Invalid expect expression: {expr!r}")


def build_checker(expr: str) -> Callable[[str, int], bool]:
    """Parse an expect expression and return a checker function.

    The checker takes (stdout_stripped, exit_code) and returns bool.
    """
    parsed = parse_expect(expr)
    t = parsed["type"]

    if t == "empty":
        return lambda out, _ec: out == ""
    if t == "not_empty":
        return lambda out, _ec: out != ""
    if t == "equals":
        val = parsed["value"]
        return lambda out, _ec: out == val
    if t == "not_equals":
        val = parsed["value"]
        return lambda out, _ec: out != val
    if t == "contains":
        val = parsed["value"]
        return lambda out, _ec: val in out
    if t == "starts_with":
        val = parsed["value"]
        return lambda out, _ec: out.startswith(val)
    if t == "ends_with":
        val = parsed["value"]
        return lambda out, _ec: out.endswith(val)
    if t == "matches":
        pattern = re.compile(parsed["value"])
        return lambda out, _ec: pattern.search(out) is not None
    if t == "greater_than":
        val = parsed["value"]
        return lambda out, _ec: _try_int(out) is not None and _try_int(out) > val
    if t == "less_than":
        val = parsed["value"]
        return lambda out, _ec: _try_int(out) is not None and _try_int(out) < val
    if t == "exit_code":
        val = parsed["value"]
        return lambda _out, ec: ec == val
    if t == "is_json":
        return lambda out, _ec: _is_valid_json(out)
    if t == "is_json_object":
        return lambda out, _ec: _is_json_type(out, dict)
    if t == "is_json_array":
        return lambda out, _ec: _is_json_type(out, list)
    if t == "matches_regex":
        pattern = re.compile(parsed["value"])
        return lambda out, _ec: pattern.fullmatch(out) is not None

    raise ValueError(f"Unknown expression type: {t}")


def _try_int(s: str) -> int | None:
    """Try to parse a string as an integer, return None on failure."""
    try:
        return int(s.strip())
    except (ValueError, TypeError):
        return None


def _is_valid_json(s: str) -> bool:
    """Check if a string is valid JSON."""
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _is_json_type(s: str, expected_type: type) -> bool:
    """Check if a string is valid JSON and the parsed value is the expected type."""
    try:
        parsed = json.loads(s)
        return isinstance(parsed, expected_type)
    except (json.JSONDecodeError, ValueError):
        return False
