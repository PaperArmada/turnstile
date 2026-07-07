"""Tests for turnstile_core.runtime.gates."""

import time
from pathlib import Path

import pytest

from turnstile_core.definition.model import CompositeValidation, Severity, ValidationRule
from turnstile_core.runtime.gates import (
    ValidationResult,
    build_checker,
    check_evidence_freshness,
    has_blocking_failures,
    run_command,
    run_validation,
    run_validation_entry,
    run_validations,
    substitute_params,
)


# ---------------------------------------------------------------------------
# build_checker
# ---------------------------------------------------------------------------


class TestBuildChecker:
    def test_empty(self):
        check = build_checker("empty")
        assert check("", 0) is True
        assert check("stuff", 0) is False

    def test_not_empty(self):
        check = build_checker("not_empty")
        assert check("stuff", 0) is True
        assert check("", 0) is False

    def test_equals(self):
        check = build_checker('equals("yes")')
        assert check("yes", 0) is True
        assert check("no", 0) is False

    def test_not_equals(self):
        check = build_checker('not_equals("main")')
        assert check("feature/x", 0) is True
        assert check("main", 0) is False

    def test_contains(self):
        check = build_checker('contains("OK")')
        assert check("all OK here", 0) is True
        assert check("nope", 0) is False

    def test_starts_with(self):
        check = build_checker('starts_with("v")')
        assert check("v1.0", 0) is True
        assert check("1.0", 0) is False

    def test_ends_with(self):
        check = build_checker('ends_with("0")')
        assert check("exit 0", 0) is True
        assert check("exit 1", 0) is False

    def test_matches(self):
        check = build_checker('matches("^\\d+$")')
        assert check("42", 0) is True
        assert check("abc", 0) is False

    def test_greater_than(self):
        check = build_checker("greater_than(5)")
        assert check("10", 0) is True
        assert check("5", 0) is False
        assert check("abc", 0) is False

    def test_less_than(self):
        check = build_checker("less_than(100)")
        assert check("50", 0) is True
        assert check("100", 0) is False

    def test_exit_code(self):
        check = build_checker("exit_code(0)")
        assert check("anything", 0) is True
        assert check("anything", 1) is False

    def test_is_json(self):
        check = build_checker("is_json")
        assert check('{"key": "value"}', 0) is True
        assert check("[1, 2, 3]", 0) is True
        assert check('"just a string"', 0) is True
        assert check("42", 0) is True
        assert check("not json at all", 0) is False
        assert check("", 0) is False
        assert check("Authentication required", 0) is False

    def test_is_json_object(self):
        check = build_checker("is_json_object")
        assert check('{"key": "value"}', 0) is True
        assert check("{}", 0) is True
        assert check("[1, 2]", 0) is False
        assert check('"string"', 0) is False
        assert check("not json", 0) is False

    def test_is_json_array(self):
        check = build_checker("is_json_array")
        assert check("[1, 2, 3]", 0) is True
        assert check("[]", 0) is True
        assert check('{"key": "value"}', 0) is False
        assert check("not json", 0) is False

    def test_matches_regex(self):
        check = build_checker('matches_regex("^\\d+$")')
        assert check("42", 0) is True
        assert check("abc", 0) is False
        # fullmatch: partial matches fail (unlike matches which uses search)
        assert check("abc 42 def", 0) is False

    def test_matches_regex_vs_matches(self):
        # matches (search) finds substring
        search_check = build_checker('matches("\\d+")')
        assert search_check("abc 42 def", 0) is True
        # matches_regex (fullmatch) requires full string match
        full_check = build_checker('matches_regex("\\d+")')
        assert full_check("abc 42 def", 0) is False
        assert full_check("42", 0) is True


# ---------------------------------------------------------------------------
# substitute_params
# ---------------------------------------------------------------------------


class TestSubstituteParams:
    def test_basic(self):
        result = substitute_params(
            "echo ${name} ${id}", {"name": "feature/auth", "id": "42"}
        )
        assert result == "echo feature/auth 42"

    def test_no_params(self):
        assert substitute_params("echo hello", {}) == "echo hello"

    def test_missing_param_left_as_is(self):
        result = substitute_params("echo ${missing}", {})
        assert result == "echo ${missing}"


# ---------------------------------------------------------------------------
# check_evidence_freshness
# ---------------------------------------------------------------------------


class TestCheckEvidenceFreshness:
    def test_missing_file(self, tmp_path):
        result = check_evidence_freshness("nofile.json", "30m", tmp_path)
        assert result is not None
        assert "not found" in result

    def test_fresh_file(self, tmp_path):
        f = tmp_path / "results.json"
        f.write_text("{}")
        result = check_evidence_freshness("results.json", "30m", tmp_path)
        assert result is None

    def test_stale_file(self, tmp_path):
        import os

        f = tmp_path / "old.json"
        f.write_text("{}")
        # Make it appear old
        old_time = time.time() - 3600  # 1 hour ago
        os.utime(f, (old_time, old_time))
        result = check_evidence_freshness("old.json", "30m", tmp_path)
        assert result is not None
        assert "old" in result.lower() or "3600" in result or "1800" in result


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------


class TestRunCommand:
    @pytest.mark.asyncio
    async def test_simple_command(self, tmp_path):
        output, code = await run_command("echo hello", tmp_path)
        assert output == "hello"
        assert code == 0

    @pytest.mark.asyncio
    async def test_exit_code(self, tmp_path):
        output, code = await run_command("exit 1", tmp_path)
        assert code == 1

    @pytest.mark.asyncio
    async def test_empty_output(self, tmp_path):
        output, code = await run_command("true", tmp_path)
        assert output == ""
        assert code == 0

    @pytest.mark.asyncio
    async def test_timeout(self, tmp_path):
        import asyncio

        with pytest.raises(asyncio.TimeoutError):
            await run_command("sleep 10", tmp_path, timeout=1)


# ---------------------------------------------------------------------------
# run_validation
# ---------------------------------------------------------------------------


class TestRunValidation:
    @pytest.mark.asyncio
    async def test_passing_rule(self, tmp_path):
        rule = ValidationRule(
            command="echo hello",
            expect="not_empty",
            message="should have output",
        )
        result = await run_validation(rule, {}, tmp_path)
        assert result.passed is True
        assert result.output == "hello"

    @pytest.mark.asyncio
    async def test_failing_rule(self, tmp_path):
        rule = ValidationRule(
            command="echo hello",
            expect="empty",
            message="should be empty",
        )
        result = await run_validation(rule, {}, tmp_path)
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_param_substitution(self, tmp_path):
        rule = ValidationRule(
            command="echo ${name}",
            expect='equals("world")',
            message="should echo param",
        )
        result = await run_validation(rule, {"name": "world"}, tmp_path)
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self, tmp_path):
        rule = ValidationRule(
            command="sleep 10",
            expect="empty",
            message="should not hang",
            timeout=1,
        )
        result = await run_validation(rule, {}, tmp_path)
        assert result.passed is False
        assert "timed out" in result.error.lower()


# ---------------------------------------------------------------------------
# run_validation_entry (composites)
# ---------------------------------------------------------------------------


class TestRunValidationEntry:
    @pytest.mark.asyncio
    async def test_any_one_passes(self, tmp_path):
        composite = CompositeValidation(
            any=[
                ValidationRule(command="echo yes", expect='equals("yes")', message="a"),
                ValidationRule(command="echo no", expect='equals("yes")', message="b"),
            ],
            message="at least one",
        )
        results = await run_validation_entry(composite, {}, tmp_path)
        assert len(results) == 2
        # At least one passed, so no blocking failures
        assert not has_blocking_failures(results)

    @pytest.mark.asyncio
    async def test_any_none_pass(self, tmp_path):
        composite = CompositeValidation(
            any=[
                ValidationRule(command="echo no", expect='equals("yes")', message="a"),
                ValidationRule(command="echo nope", expect='equals("yes")', message="b"),
            ],
            message="need one",
        )
        results = await run_validation_entry(composite, {}, tmp_path)
        assert has_blocking_failures(results)

    @pytest.mark.asyncio
    async def test_all_pass(self, tmp_path):
        composite = CompositeValidation(
            all=[
                ValidationRule(
                    command="echo yes", expect='equals("yes")', message="a"
                ),
                ValidationRule(command="echo yes", expect="not_empty", message="b"),
            ],
            message="all must pass",
        )
        results = await run_validation_entry(composite, {}, tmp_path)
        assert not has_blocking_failures(results)

    @pytest.mark.asyncio
    async def test_all_one_fails(self, tmp_path):
        composite = CompositeValidation(
            all=[
                ValidationRule(
                    command="echo yes", expect='equals("yes")', message="a"
                ),
                ValidationRule(
                    command="echo no", expect='equals("yes")', message="b"
                ),
            ],
            message="all must pass",
        )
        results = await run_validation_entry(composite, {}, tmp_path)
        assert has_blocking_failures(results)


# ---------------------------------------------------------------------------
# run_validations + has_blocking_failures
# ---------------------------------------------------------------------------


class TestRunValidations:
    @pytest.mark.asyncio
    async def test_mixed_severity(self, tmp_path):
        entries = [
            ValidationRule(
                command="echo ok",
                expect="not_empty",
                message="passes",
                severity=Severity.error,
            ),
            ValidationRule(
                command="echo bad",
                expect="empty",
                message="fails but warning",
                severity=Severity.warning,
            ),
        ]
        results = await run_validations(entries, {}, tmp_path)
        assert len(results) == 2
        # The warning failure should not block
        assert not has_blocking_failures(results)

    @pytest.mark.asyncio
    async def test_error_blocks(self, tmp_path):
        entries = [
            ValidationRule(
                command="echo bad",
                expect="empty",
                message="fails with error",
                severity=Severity.error,
            ),
        ]
        results = await run_validations(entries, {}, tmp_path)
        assert has_blocking_failures(results)
