"""on_exit validations scoped to particular transitions (GH #45).

A state's on_exit validations run on every transition out of it. An
evidence gate meaning "proven before advancing" (verify -> done) therefore
also refused the retreat (verify -> fix), the one move needed while the
evidence is red. `only_to` / `except_to` on an on_exit entry name the
target states the entry guards; entries without them keep running on every
transition. Asserted end to end through the Engine, for both the ordinary
transition path and the signal-driven one.
"""

from pathlib import Path

import pytest
import yaml

from turnstile_core.admin import simulate_dry_run
from turnstile_core.engine import Engine
from turnstile_core.errors import InheritanceError
from turnstile_core.inheritance import resolve_inheritance
from turnstile_core.models import (
    OverrideSpec,
    ProcessDefinition,
    ProcessOverride,
    StatePatch,
)

# Every gate below fails when it runs ("red" is not empty), so whether a
# transition succeeds shows whether the gate ran for that target.
DEFINITION = """
name: scoped
version: "1.0.0"
states:
  - id: start
    type: initial
    transitions: [only, except, unscoped, group, awaiting]

  - id: only
    transitions: [done, back]
    on_exit:
      validate:
        - command: "echo red"
          expect: empty
          message: "suite green before advancing"
          only_to: [done]

  - id: except
    transitions: [done, back]
    on_exit:
      validate:
        - command: "echo red"
          expect: empty
          message: "suite green unless retreating"
          except_to: [back]

  - id: unscoped
    transitions: [done, back]
    on_exit:
      validate:
        - command: "echo red"
          expect: empty
          message: "runs on every transition"

  - id: group
    transitions: [done, back]
    on_exit:
      validate:
        - all:
            - command: "echo red"
              expect: empty
          message: "composite scoped as a whole"
          only_to: [done]

  - id: awaiting
    type: wait
    signal:
      name: reviewed
    transitions: [done, back]
    on_exit:
      validate:
        - command: "echo red"
          expect: empty
          message: "approval evidence"
          only_to: [done]

  - id: back
    transitions: [done]

  - id: done
    type: terminal
"""


@pytest.fixture()
def project(tmp_path) -> Path:
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "scoped.yaml").write_text(DEFINITION)
    return tmp_path


@pytest.fixture()
def engine(project: Path) -> Engine:
    return Engine(project)


async def _at(engine: Engine, state: str) -> str:
    iid = engine.start("scoped")["instance_id"]
    result = await engine.transition(iid, state)
    assert result.success, result.message
    return iid


class TestTransitionPath:
    @pytest.mark.asyncio
    async def test_only_to_gate_does_not_run_on_another_target(self, engine):
        iid = await _at(engine, "only")
        result = await engine.transition(iid, "back")
        assert result.success
        assert result.validation_results == []

    @pytest.mark.asyncio
    async def test_only_to_gate_blocks_its_target(self, engine):
        iid = await _at(engine, "only")
        result = await engine.transition(iid, "done")
        assert not result.success
        assert result.message == "on_exit validation failed"

    @pytest.mark.asyncio
    async def test_except_to_gate_does_not_run_on_the_excepted_target(
        self, engine
    ):
        iid = await _at(engine, "except")
        result = await engine.transition(iid, "back")
        assert result.success

    @pytest.mark.asyncio
    async def test_except_to_gate_blocks_other_targets(self, engine):
        iid = await _at(engine, "except")
        result = await engine.transition(iid, "done")
        assert not result.success

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["done", "back"])
    async def test_unscoped_gate_still_runs_on_every_transition(
        self, engine, target
    ):
        iid = await _at(engine, "unscoped")
        result = await engine.transition(iid, target)
        assert not result.success

    @pytest.mark.asyncio
    async def test_scoped_composite_skips_other_targets(self, engine):
        iid = await _at(engine, "group")
        assert (await engine.transition(iid, "back")).success

    @pytest.mark.asyncio
    async def test_scoped_composite_blocks_its_target(self, engine):
        iid = await _at(engine, "group")
        assert not (await engine.transition(iid, "done")).success


class TestSignalPath:
    @pytest.mark.asyncio
    async def test_signal_to_another_target_skips_the_gate(self, engine):
        iid = await _at(engine, "awaiting")
        result = await engine.receive_signal(iid, "reviewed", {}, "back")
        assert result["success"]
        assert result["new_state"] == "back"

    @pytest.mark.asyncio
    async def test_signal_to_the_gated_target_is_refused(self, engine):
        iid = await _at(engine, "awaiting")
        result = await engine.receive_signal(iid, "reviewed", {}, "done")
        assert not result["success"]
        assert result["new_state"] == "awaiting"


# ---------------------------------------------------------------------------
# Definition validation: a scope that cannot mean what it says is rejected
# at load time, since a typo would otherwise silently switch a gate off.
# ---------------------------------------------------------------------------


def _with_check(on_exit=None, on_enter=None) -> dict:
    data = yaml.safe_load(DEFINITION)
    check = {"id": "check", "transitions": ["done", "back"]}
    if on_exit is not None:
        check["on_exit"] = {"validate": on_exit}
    if on_enter is not None:
        check["on_enter"] = {"validate": on_enter}
    data["states"].append(check)
    return data


RED = {"command": "echo red", "expect": "empty"}


def test_scoped_definition_loads():
    ProcessDefinition(**_with_check(on_exit=[{**RED, "only_to": ["done"]}]))


@pytest.mark.parametrize(
    "entry, error",
    [
        ({**RED, "only_to": ["dnoe"]}, "'dnoe'"),
        ({**RED, "except_to": ["fix"]}, "'fix'"),
        ({**RED, "only_to": []}, "at least one"),
        ({**RED, "only_to": ["done"], "except_to": ["back"]}, "not both"),
        ({"all": [{**RED, "only_to": ["done"]}]}, "inside"),
    ],
    ids=["unknown-only", "unknown-except", "empty", "both", "nested-rule"],
)
def test_bad_scope_is_rejected(entry, error):
    with pytest.raises(ValueError, match=error):
        ProcessDefinition(**_with_check(on_exit=[entry]))


def test_scope_on_an_on_enter_gate_is_rejected():
    with pytest.raises(ValueError, match="on_exit"):
        ProcessDefinition(**_with_check(on_enter=[{**RED, "only_to": ["done"]}]))


def test_dry_run_describes_the_scope():
    defn = ProcessDefinition(**yaml.safe_load(DEFINITION))
    states = {s["state"]: s for s in simulate_dry_run(defn)}
    assert states["only"]["on_exit_validations"][0]["only_to"] == ["done"]
    assert states["except"]["on_exit_validations"][0]["except_to"] == ["back"]
    assert states["group"]["on_exit_validations"][0]["only_to"] == ["done"]


@pytest.mark.parametrize("target, gates", [("done", 1), ("back", 0)])
def test_dry_run_path_lists_only_the_gates_for_that_step(target, gates):
    defn = ProcessDefinition(**yaml.safe_load(DEFINITION))
    step = simulate_dry_run(defn, ["only", target])[1]
    assert len(step["on_exit_validations"]) == gates


def _subprocess_definition(only_to: list[str]) -> dict:
    return {
        "name": "parent",
        "version": "1.0.0",
        "states": [
            {"id": "start", "type": "initial", "transitions": ["run"]},
            {
                "id": "run",
                "type": "subprocess",
                "process": "child",
                "subprocess_routing": {"on_complete": ["done"], "on_fail": ["rollback"]},
                "on_exit": {"validate": [{**RED, "only_to": only_to}]},
            },
            {"id": "rollback", "transitions": ["done"]},
            {"id": "done", "type": "terminal"},
        ],
    }


def test_subprocess_scope_is_checked_against_routing_targets():
    ProcessDefinition(**_subprocess_definition(["rollback"]))
    with pytest.raises(ValueError, match="'rolback'"):
        ProcessDefinition(**_subprocess_definition(["rolback"]))


# ---------------------------------------------------------------------------
# Inheritance: the merged definition keeps the scope and re-checks it
# ---------------------------------------------------------------------------


def _override(transitions: list[str]) -> ProcessOverride:
    return ProcessOverride(
        extends="scoped",
        overrides=OverrideSpec(
            patch_states=[StatePatch(id="only", transitions=transitions)]
        ),
    )


def test_override_keeps_the_scope():
    parent = ProcessDefinition(**yaml.safe_load(DEFINITION))
    merged = resolve_inheritance(_override(["done", "back"]), parent)
    gate = merged.get_state("only").on_exit.validations[0]
    assert gate.only_to == ["done"]
    assert not gate.applies_to("back")


def test_override_removing_a_scoped_target_is_rejected():
    parent = ProcessDefinition(**yaml.safe_load(DEFINITION))
    with pytest.raises(InheritanceError, match="'done'"):
        resolve_inheritance(_override(["back"]), parent)
