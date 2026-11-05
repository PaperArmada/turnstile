"""A blank parameter value counts as not provided (#51).

An explicitly empty or whitespace-only value used to be kept as given,
because defaults applied only when the key was absent. For the starter
`test_command` that meant `eval ""`, which exits 0, so the blocking
"Test suite must pass" gates passed without running a test. A blank value
now takes the parameter's default, like the shell's `${name:-default}`,
and a blank required parameter is refused as missing. This holds for
`start`, for children started by subprocess and dispatch states, and for
the parameters gates see, so an instance stored with a blank value before
the fix does not keep the bypass.
"""

from pathlib import Path

import pytest

from turnstile_core.engine import Engine
from turnstile_core.errors import DefinitionError, SubprocessError

CHILD = """\
name: child
version: "1.0.0"
parameters:
  - name: test_command
    required: false
    default: "pytest"
  - name: label
    required: false
states:
  - id: start
    type: initial
    transitions: [done]
  - id: done
    type: terminal
"""

STRICT_CHILD = """\
name: strict-child
version: "1.0.0"
parameters:
  - name: target
    required: true
states:
  - id: start
    type: initial
    transitions: [done]
  - id: done
    type: terminal
"""

PARENT = """\
name: parent
version: "1.0.0"
parameters:
  - name: forwarded
    required: false
    default: "   "
states:
  - id: start
    type: initial
    transitions: [delegate, fan_out, strict, strict_fan_out]
  - id: delegate
    type: subprocess
    process: child
    parameter_map:
      test_command: "${forwarded}"
    subprocess_routing:
      on_complete: [done]
      on_fail: [done]
  - id: fan_out
    type: dispatch
    process: child
    parameter_map:
      test_command: "${forwarded}"
    immediate: done
  - id: strict
    type: subprocess
    process: strict-child
    parameter_map:
      target: "${forwarded}"
    subprocess_routing:
      on_complete: [done]
      on_fail: [done]
  - id: strict_fan_out
    type: dispatch
    process: strict-child
    parameter_map:
      target: "${forwarded}"
    immediate: done
  - id: done
    type: terminal
"""


@pytest.fixture()
def engine(tmp_path: Path) -> Engine:
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "child.yaml").write_text(CHILD)
    (proc_dir / "strict-child.yaml").write_text(STRICT_CHILD)
    (proc_dir / "parent.yaml").write_text(PARENT)
    return Engine(tmp_path)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_start_gives_a_blank_value_the_default(engine, blank):
    result = engine.start("child", {"test_command": blank})
    assert result["parameters"]["test_command"] == "pytest"


def test_start_keeps_a_non_blank_value_exactly(engine):
    result = engine.start("child", {"test_command": " make test "})
    assert result["parameters"]["test_command"] == " make test "


def test_start_keeps_a_blank_value_without_a_default(engine):
    result = engine.start("child", {"label": ""})
    assert result["parameters"]["label"] == ""


@pytest.mark.parametrize("blank", ["", "  "])
def test_start_refuses_a_blank_required_parameter(engine, blank):
    with pytest.raises(DefinitionError, match="Required parameter 'target'"):
        engine.start("strict-child", {"target": blank})


@pytest.mark.asyncio
async def test_subprocess_child_gives_a_blank_mapped_value_the_default(engine):
    parent = engine.start("parent")
    result = await engine.transition(parent["instance_id"], "delegate")
    child = engine._store.load(result.subprocess_started)
    assert child.parameters["test_command"] == "pytest"


@pytest.mark.asyncio
async def test_dispatch_child_gives_a_blank_mapped_value_the_default(engine):
    parent = engine.start("parent")
    result = await engine.transition(parent["instance_id"], "fan_out")
    child = engine._store.load(result.subprocess_started)
    assert child.parameters["test_command"] == "pytest"


@pytest.mark.asyncio
async def test_subprocess_refuses_a_blank_required_child_parameter(engine):
    parent = engine.start("parent")
    with pytest.raises(SubprocessError, match="'target'"):
        await engine.transition(parent["instance_id"], "strict")


@pytest.mark.asyncio
async def test_dispatch_refuses_a_blank_required_child_parameter(engine):
    parent = engine.start("parent")
    with pytest.raises(SubprocessError, match="'target'"):
        await engine.transition(parent["instance_id"], "strict_fan_out")


@pytest.mark.asyncio
async def test_blank_dispatch_metadata_keeps_the_parent_value(engine):
    parent = engine.start("parent", {"forwarded": "make test"})
    result = await engine.transition(
        parent["instance_id"], "fan_out", metadata={"forwarded": ""}
    )
    child = engine._store.load(result.subprocess_started)
    assert child.parameters["test_command"] == "make test"


@pytest.mark.parametrize("stored", [{"test_command": ""}, {}])
def test_gates_see_the_default_for_a_stored_blank_or_missing_value(
    engine, stored
):
    """An instance stored by an older engine, or before its definition
    declared the parameter, still exposes the default to gates."""
    defn, def_hash = engine._get_definition("child")
    instance = engine._store.create(
        process_name="child",
        initial_state="start",
        version=defn.version,
        definition_hash=def_hash,
        parameters=stored,
    )
    engine._store.save(instance)
    reloaded = engine._store.load(instance.instance_id)
    assert engine._gate_params(reloaded, defn)["test_command"] == "pytest"
