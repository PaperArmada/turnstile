"""Tests that process parameters cannot inject shell commands into gates.

Gate commands are authored in a process definition and are intentionally
arbitrary shell. Parameter *values*, however, may come from a less-trusted
source than the definition author: a shared or third-party definition invoked
with caller-supplied parameters. The boundary asserted here is that a
parameter value can never become executable shell syntax.

Parameters are passed to the shell as environment variables rather than being
interpolated into the command text. Note that shlex.quote is NOT a sufficient
alternative: it only protects a placeholder that is bare in the template, and
for the more natural `echo "${var}"` the injected quotes land inside the
author's quotes and the payload still escapes. Both quoting styles are
therefore exercised below.
"""

import shutil
from pathlib import Path

import pytest

from turnstile_core.engine import Engine
from turnstile_core.validator import run_command

FIXTURES = Path(__file__).parent / "fixtures"

# Payloads that each attempt to create a marker file via a different shell
# escape mechanism.
PAYLOADS = [
    ('double_quote', 'x"; touch {marker}; echo "'),
    ('single_quote', "x'; touch {marker}; echo '"),
    ('semicolon', 'x; touch {marker}'),
    ('command_subst', 'x$(touch {marker})'),
    ('backtick', 'x`touch {marker}`'),
]

# The same placeholder written three ways in the gate command.
QUOTING_STYLES = ['quoted', 'unquoted', 'single_quoted']


@pytest.fixture()
def project(tmp_path) -> Path:
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(FIXTURES / "injection.yaml", proc_dir / "injection.yaml")
    return tmp_path


@pytest.fixture()
def engine(project: Path) -> Engine:
    return Engine(project)


@pytest.mark.parametrize("style", QUOTING_STYLES)
@pytest.mark.parametrize("name,template", PAYLOADS)
@pytest.mark.asyncio
async def test_parameter_value_cannot_execute_commands(
    engine: Engine, project: Path, style: str, name: str, template: str
):
    marker = f"PWNED_{style}_{name}"
    payload = template.format(marker=marker)

    iid = engine.start("injection", {"v": payload})["instance_id"]
    await engine.transition(iid, style)

    assert not (project / marker).exists(), (
        f"parameter value executed a command via {name} in a {style} placeholder"
    )


@pytest.mark.asyncio
async def test_parameter_value_reaches_the_gate_intact(
    engine: Engine, project: Path
):
    """Hardening must not corrupt the value the gate actually sees.

    A fix that mangles or drops the value would defeat the injection tests
    without preserving the feature, so pin the round trip explicitly.
    """
    payload = 'x"; touch NOT_CREATED; echo "'

    iid = engine.start("injection", {"v": payload})["instance_id"]
    result = await engine.transition(iid, "quoted")

    assert result.validation_results[0]["output"] == payload


@pytest.mark.asyncio
async def test_non_identifier_parameter_name_is_not_substituted(tmp_path):
    """Names that cannot be exported must not fall back to raw substitution.

    'my-param' is not a valid shell identifier, so it cannot be passed through
    the environment. Substituting it into the command text as a fallback would
    reintroduce the injection for exactly the names that cannot be passed
    safely, so the placeholder is left alone instead.
    """
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    shutil.copy(FIXTURES / "injection-hyphen.yaml", proc_dir / "hyphen.yaml")
    e = Engine(tmp_path)

    iid = e.start("injection-hyphen", {"my-param": 'x"; touch HYPHEN_PWNED; echo "'})[
        "instance_id"
    ]
    await e.transition(iid, "checked")

    assert not (tmp_path / "HYPHEN_PWNED").exists()


class TestRunCommandEnvironment:
    @pytest.mark.asyncio
    async def test_parameters_are_exported_to_the_shell(self, tmp_path):
        out, code = await run_command(
            'echo "${greeting}"', tmp_path, parameters={"greeting": "hello"}
        )
        assert out == "hello"
        assert code == 0

    @pytest.mark.asyncio
    async def test_non_identifier_names_are_not_exported(self, tmp_path):
        """An unexportable name leaves its placeholder unexpanded, not filled in."""
        out, _ = await run_command(
            'echo "${bad-name}"', tmp_path, parameters={"bad-name": "value"}
        )
        assert "value" not in out

    @pytest.mark.asyncio
    async def test_ambient_environment_is_preserved(self, tmp_path):
        """Gates rely on PATH and friends; the env must be extended, not replaced."""
        out, _ = await run_command("echo $PATH", tmp_path, parameters={"x": "1"})
        assert out.strip() != ""
