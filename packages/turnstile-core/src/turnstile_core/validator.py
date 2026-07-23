"""Expression parser and shell command runner for validation gates."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from turnstile_core.models import (
    CompositeValidation,
    Severity,
    ValidationEntry,
    ValidationRule,
    parse_expect,
)


@dataclass
class ValidationResult:
    """Result of running a single validation."""

    command: str
    expect: str
    passed: bool
    output: str = ""
    exit_code: int = 0
    message: str = ""
    severity: Severity = Severity.error
    error: str = ""
    elapsed_ms: int = 0


# ---------------------------------------------------------------------------
# Expression checkers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Parameter substitution
# ---------------------------------------------------------------------------


def substitute_params(template: str, parameters: dict[str, str]) -> str:
    """Replace ${var_name} placeholders in a plain string.

    This performs no escaping and is therefore only safe for values that are
    NOT handed to a shell: child process parameters, messages, and similar
    data. Shell commands must never be built this way; pass the parameters to
    run_command instead, which exports them to the environment.
    """
    result = template
    for key, value in parameters.items():
        result = result.replace(f"${{{key}}}", str(value))
    return result


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# Evidence-based validation
# ---------------------------------------------------------------------------


def _parse_duration(duration_str: str) -> float:
    """Parse a duration string like '30m', '1h', '90s' into seconds."""
    duration_str = duration_str.strip().lower()
    if duration_str.endswith("m"):
        return float(duration_str[:-1]) * 60
    if duration_str.endswith("h"):
        return float(duration_str[:-1]) * 3600
    if duration_str.endswith("s"):
        return float(duration_str[:-1])
    return float(duration_str)


def check_evidence_freshness(
    evidence_path: str, max_age: str, cwd: Path
) -> str | None:
    """Check if an evidence file exists and is fresh enough.

    Returns None if OK, or an error message if stale/missing.
    """
    path = cwd / evidence_path
    if not path.exists():
        return f"Evidence file not found: {evidence_path}"

    max_age_seconds = _parse_duration(max_age)
    file_age = time.time() - path.stat().st_mtime
    if file_age > max_age_seconds:
        return (
            f"Evidence file '{evidence_path}' is {file_age:.0f}s old, "
            f"max allowed is {max_age_seconds:.0f}s"
        )
    return None


# ---------------------------------------------------------------------------
# Shell command execution
# ---------------------------------------------------------------------------


async def run_command(
    command: str,
    cwd: Path,
    timeout: int = 60,
    parameters: dict[str, str] | None = None,
    merge_stderr: bool = False,
) -> tuple[str, int]:
    """Run a shell command and return (stdout_stripped, exit_code).

    With ``merge_stderr`` the child's stderr is interleaved into stdout at
    the pipe level, so the returned text includes diagnostics (tracebacks,
    "command not found"). Validation gates keep the default separation so
    ``expect`` matching is not perturbed by stderr noise.

    Process parameters are supplied to the shell as environment variables
    rather than being interpolated into the command text. The shell expands
    the ${var} references itself, so a parameter value is never re-parsed as
    shell syntax and cannot inject commands.

    Interpolating the value into the command text is not a safe alternative,
    even with shlex.quote. Quoting only protects a placeholder that is bare in
    the template; for the far more natural `echo "${var}"` the injected quotes
    land inside the author's quotes and a value such as 'x"; rm -rf .; echo "'
    still escapes. Passing through the environment removes the class of bug
    rather than narrowing it.

    Parameter names that are not valid shell identifiers (for example
    'my-param') cannot be exported and are skipped. Their placeholders are
    left in the command rather than being filled in literally, because
    substituting them would reintroduce the injection for the one class of
    name that cannot be passed safely.

    Raises asyncio.TimeoutError if the command exceeds the timeout.
    """
    env = os.environ.copy()
    if parameters:
        for key, value in parameters.items():
            if _ENV_NAME_RE.match(key):
                env[key] = str(value)

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=(
            asyncio.subprocess.STDOUT if merge_stderr
            else asyncio.subprocess.PIPE
        ),
        cwd=str(cwd),
        env=env,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise

    # Lossy decode: a command that emits non-UTF-8 bytes should surface as
    # garbled output, not raise UnicodeDecodeError out of the runner.
    return stdout.decode(errors="replace").strip(), proc.returncode or 0


# ---------------------------------------------------------------------------
# Validation execution
# ---------------------------------------------------------------------------


async def run_validation(
    rule: ValidationRule,
    parameters: dict[str, str],
    cwd: Path,
) -> ValidationResult:
    """Execute a single validation rule and return the result."""
    command = rule.command
    start = time.monotonic()

    # Evidence-based: check freshness first
    if rule.evidence and rule.max_age:
        freshness_error = check_evidence_freshness(rule.evidence, rule.max_age, cwd)
        if freshness_error:
            elapsed = int((time.monotonic() - start) * 1000)
            return ValidationResult(
                command=command,
                expect=rule.expect,
                passed=False,
                message=rule.message,
                severity=rule.severity,
                error=freshness_error,
                elapsed_ms=elapsed,
            )

    try:
        output, exit_code = await run_command(
            command, cwd, timeout=rule.timeout, parameters=parameters
        )
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start) * 1000)
        return ValidationResult(
            command=command,
            expect=rule.expect,
            passed=False,
            message=rule.message,
            severity=rule.severity,
            error=f"Command timed out after {rule.timeout}s",
            elapsed_ms=elapsed,
        )
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return ValidationResult(
            command=command,
            expect=rule.expect,
            passed=False,
            message=rule.message,
            severity=rule.severity,
            error=str(e),
            elapsed_ms=elapsed,
        )

    checker = build_checker(rule.expect)
    passed = checker(output, exit_code)
    elapsed = int((time.monotonic() - start) * 1000)

    return ValidationResult(
        command=command,
        expect=rule.expect,
        passed=passed,
        output=output,
        exit_code=exit_code,
        message=rule.message,
        severity=rule.severity,
        elapsed_ms=elapsed,
    )


async def run_validation_entry(
    entry: ValidationEntry,
    parameters: dict[str, str],
    cwd: Path,
) -> list[ValidationResult]:
    """Run a single validation entry (simple rule or composite)."""
    if isinstance(entry, ValidationRule):
        return [await run_validation(entry, parameters, cwd)]

    if isinstance(entry, CompositeValidation):
        rules = entry.any_of or entry.all_of or []
        results = []
        for rule in rules:
            results.append(await run_validation(rule, parameters, cwd))

        if entry.any_of is not None:
            # OR: at least one must pass
            if not any(r.passed for r in results):
                for r in results:
                    if not r.passed:
                        r.message = entry.message or r.message
            # If any passed, mark failures as info (they didn't block)
            else:
                for r in results:
                    if not r.passed:
                        r.severity = Severity.info
        # all_of: default AND behavior (all must pass) is the natural result

        return results

    raise ValueError(f"Unknown validation entry type: {type(entry)}")


async def run_validations(
    entries: list[ValidationEntry],
    parameters: dict[str, str],
    cwd: Path,
) -> list[ValidationResult]:
    """Run all validation entries and return all results."""
    results: list[ValidationResult] = []
    for entry in entries:
        results.extend(await run_validation_entry(entry, parameters, cwd))
    return results


def has_blocking_failures(results: list[ValidationResult]) -> bool:
    """Check if any results have severity=error and passed=False."""
    return any(
        not r.passed and r.severity == Severity.error for r in results
    )
