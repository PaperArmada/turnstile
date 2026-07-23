"""Tests for turnstile_core.models."""

from pathlib import Path

import pytest
import yaml

from turnstile_core.models import (
    CompositeValidation,
    ProcessDefinition,
    ProcessParameter,
    ProcessState,
    RegistryConfig,
    Severity,
    StateType,
    ValidationRule,
    parse_expect,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# parse_expect
# ---------------------------------------------------------------------------


class TestParseExpect:
    def test_bare_empty(self):
        assert parse_expect("empty") == {"type": "empty"}

    def test_bare_not_empty(self):
        assert parse_expect("not_empty") == {"type": "not_empty"}

    def test_equals(self):
        assert parse_expect('equals("yes")') == {"type": "equals", "value": "yes"}

    def test_not_equals(self):
        assert parse_expect('not_equals("main")') == {
            "type": "not_equals",
            "value": "main",
        }

    def test_contains(self):
        assert parse_expect('contains("OK")') == {"type": "contains", "value": "OK"}

    def test_starts_with(self):
        assert parse_expect('starts_with("v")') == {
            "type": "starts_with",
            "value": "v",
        }

    def test_ends_with(self):
        assert parse_expect('ends_with("0")') == {"type": "ends_with", "value": "0"}

    def test_matches(self):
        assert parse_expect('matches("^\\d+$")') == {
            "type": "matches",
            "value": "^\\d+$",
        }

    def test_greater_than(self):
        assert parse_expect("greater_than(0)") == {
            "type": "greater_than",
            "value": 0,
        }

    def test_less_than(self):
        assert parse_expect("less_than(100)") == {"type": "less_than", "value": 100}

    def test_exit_code(self):
        assert parse_expect("exit_code(0)") == {"type": "exit_code", "value": 0}

    def test_whitespace_stripped(self):
        assert parse_expect("  empty  ") == {"type": "empty"}

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid expect expression"):
            parse_expect("bogus()")


# ---------------------------------------------------------------------------
# ValidationRule
# ---------------------------------------------------------------------------


class TestValidationRule:
    def test_basic(self):
        rule = ValidationRule(
            command="echo hello",
            expect="not_empty",
            message="should produce output",
        )
        assert rule.severity == Severity.error
        assert rule.timeout == 60

    def test_invalid_expect_rejected(self):
        with pytest.raises(Exception):
            ValidationRule(command="echo", expect="invalid_func()", message="bad")


# ---------------------------------------------------------------------------
# CompositeValidation
# ---------------------------------------------------------------------------


class TestCompositeValidation:
    def test_any(self):
        rules = [
            ValidationRule(command="echo 1", expect="not_empty", message="a"),
            ValidationRule(command="echo 2", expect="not_empty", message="b"),
        ]
        cv = CompositeValidation(any=rules, message="one must pass")
        assert cv.any_of is not None
        assert cv.all_of is None

    def test_all(self):
        rules = [
            ValidationRule(command="echo 1", expect="not_empty", message="a"),
        ]
        cv = CompositeValidation(all=rules, message="all must pass")
        assert cv.all_of is not None
        assert cv.any_of is None

    def test_neither_raises(self):
        with pytest.raises(Exception):
            CompositeValidation(message="nothing")

    def test_both_raises(self):
        rules = [ValidationRule(command="echo", expect="empty", message="x")]
        with pytest.raises(Exception):
            CompositeValidation(any=rules, all=rules, message="both")


# ---------------------------------------------------------------------------
# ProcessDefinition
# ---------------------------------------------------------------------------


class TestProcessDefinition:
    def test_minimal_valid(self):
        defn = ProcessDefinition(
            name="test",
            states=[
                ProcessState(id="start", type=StateType.initial, transitions=["done"]),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        assert defn.initial_state().id == "start"
        assert defn.get_state("done").type == StateType.terminal

    def test_no_initial_state_rejected(self):
        with pytest.raises(Exception, match="exactly one initial state"):
            ProcessDefinition(
                name="bad",
                states=[ProcessState(id="done", type=StateType.terminal)],
            )

    def test_no_terminal_state_rejected(self):
        with pytest.raises(Exception, match="at least one terminal state"):
            ProcessDefinition(
                name="bad",
                states=[
                    ProcessState(
                        id="start", type=StateType.initial, transitions=["start"]
                    )
                ],
            )

    def test_unknown_transition_target_rejected(self):
        with pytest.raises(Exception, match="unknown state 'nowhere'"):
            ProcessDefinition(
                name="bad",
                states=[
                    ProcessState(
                        id="start", type=StateType.initial, transitions=["nowhere"]
                    ),
                    ProcessState(id="done", type=StateType.terminal),
                ],
            )

    def test_terminal_with_transitions_rejected(self):
        with pytest.raises(Exception, match="must not have transitions"):
            ProcessDefinition(
                name="bad",
                states=[
                    ProcessState(
                        id="start", type=StateType.initial, transitions=["done"]
                    ),
                    ProcessState(
                        id="done", type=StateType.terminal, transitions=["start"]
                    ),
                ],
            )

    def test_get_state_returns_none_for_missing(self):
        defn = ProcessDefinition(
            name="test",
            states=[
                ProcessState(id="start", type=StateType.initial, transitions=["done"]),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        assert defn.get_state("nonexistent") is None

    def test_dispatch_immediate_to_unknown_state_rejected(self):
        """A typo'd dispatch immediate target fails at load, not mid-process
        (GH #30)."""
        with pytest.raises(
            Exception,
            match=(
                "Dispatch state 'fire' immediate target 'nowhere' "
                "references unknown state"
            ),
        ):
            ProcessDefinition(
                name="bad",
                states=[
                    ProcessState(
                        id="start", type=StateType.initial, transitions=["fire"]
                    ),
                    ProcessState(
                        id="fire",
                        type=StateType.dispatch,
                        process="child",
                        immediate="nowhere",
                    ),
                    ProcessState(id="done", type=StateType.terminal),
                ],
            )

    def test_dispatch_immediate_to_existing_state_accepted(self):
        defn = ProcessDefinition(
            name="ok",
            states=[
                ProcessState(
                    id="start", type=StateType.initial, transitions=["fire"]
                ),
                ProcessState(
                    id="fire",
                    type=StateType.dispatch,
                    process="child",
                    immediate="done",
                ),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        assert defn.get_state("fire").immediate == "done"


# ---------------------------------------------------------------------------
# Feature-deploy fixture: full round-trip from YAML
# ---------------------------------------------------------------------------


class TestFeatureDeployFixture:
    @pytest.fixture()
    def defn(self) -> ProcessDefinition:
        raw = yaml.safe_load((FIXTURES / "feature-deploy.yaml").read_text())
        return ProcessDefinition(**raw)

    def test_loads(self, defn: ProcessDefinition):
        assert defn.name == "feature-deploy"
        assert defn.version == "1.2.0"

    def test_state_count(self, defn: ProcessDefinition):
        assert len(defn.states) == 9

    def test_initial_state(self, defn: ProcessDefinition):
        assert defn.initial_state().id == "start"

    def test_terminal_state(self, defn: ProcessDefinition):
        done = defn.get_state("done")
        assert done is not None
        assert done.type == StateType.terminal

    def test_parameters(self, defn: ProcessDefinition):
        assert len(defn.parameters) == 2
        branch_param = defn.parameters[0]
        assert branch_param.name == "branch_name"
        assert branch_param.required is True
        ticket_param = defn.parameters[1]
        assert ticket_param.required is False
        assert ticket_param.default == "none"

    def test_validation_rules_parsed(self, defn: ProcessDefinition):
        create_branch = defn.get_state("create_branch")
        assert create_branch.on_enter is not None
        assert len(create_branch.on_enter.validations) == 1
        rule = create_branch.on_enter.validations[0]
        assert isinstance(rule, ValidationRule)
        assert rule.expect == "empty"

    def test_on_exit_validations(self, defn: ProcessDefinition):
        run_tests = defn.get_state("run_tests")
        assert run_tests.on_exit is not None
        assert len(run_tests.on_exit.validations) == 2
        warning_rule = run_tests.on_exit.validations[1]
        assert isinstance(warning_rule, ValidationRule)
        assert warning_rule.severity == Severity.warning

    def test_action_hooks(self, defn: ProcessDefinition):
        done = defn.get_state("done")
        assert done.on_enter is not None
        assert len(done.on_enter.actions) == 1
        assert "${branch_name}" in done.on_enter.actions[0].command

    def test_metadata(self, defn: ProcessDefinition):
        assert defn.metadata.author == "matt"
        assert "deployment" in defn.metadata.tags

    def test_transitions(self, defn: ProcessDefinition):
        implement = defn.get_state("implement")
        assert set(implement.transitions) == {"write_tests", "needs_design_review"}


# ---------------------------------------------------------------------------
# RegistryConfig
# ---------------------------------------------------------------------------


class TestRegistryConfig:
    def test_defaults(self):
        reg = RegistryConfig()
        assert reg.version == "1.0"
        assert reg.settings.state_dir == ".process-state"
        assert reg.settings.require_override_reason is True

    def test_from_dict(self):
        data = {
            "version": "1.0",
            "local": ["feature-deploy", "hotfix"],
            "settings": {
                "state_dir": ".process-state",
                "log_retention_days": 30,
            },
        }
        reg = RegistryConfig(**data)
        assert reg.local == ["feature-deploy", "hotfix"]
        assert reg.settings.log_retention_days == 30
