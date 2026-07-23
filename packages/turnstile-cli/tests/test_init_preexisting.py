"""`turnstile init` must register pre-existing definitions (GH #40, bug A).

A definition file already present in .processes/ before init (e.g. the
quickstart's feature-deploy.yaml) was not added to the generated
registry's `local:` list. Because loader auto-discovery is disabled when
registry.local is non-empty, the process became unstartable: `list`
omitted it and the engine raised ProcessNotFoundError.

Contract pinned here: init's generated registry includes pre-existing
definition file stems in `local:` (excluding registry.yaml and stems
that collide with starter filenames; top-level override files ARE
included, since nothing else would resolve them once auto-discovery is
off), and the init output mentions what it registered. Guard-rails: no
duplicate entries, unloadable files are skipped with a warning, hostile
filenames cannot corrupt the registry, and a clean-dir init still
produces the starter-only registry.
"""

from pathlib import Path

import yaml
from click.testing import CliRunner

from turnstile_cli.main import cli
from turnstile_cli.starters import STARTER_REGISTRY, STARTERS

REPO_ROOT = Path(__file__).resolve().parents[3]

STARTER_STEMS = {name[:-5] for name in STARTERS}  # strip .yaml

CUSTOM_DEF = (
    "name: custom-proc\n"
    "version: 1.0.0\n"
    "description: pre-existing local process\n"
    "states:\n"
    "  - id: start\n"
    "    type: initial\n"
    "    transitions: [done]\n"
    "  - id: done\n"
    "    type: terminal\n"
)

# Override file: has both 'extends' and 'overrides', so
# turnstile_core.loader._is_override_file classifies it as an override.
# Named (has 'name:'), so it derives a new process from its parent.
OVERRIDE_DEF = (
    "extends: custom-proc\n"
    "name: tweaked-proc\n"
    "description: derived local process\n"
    "version: 1.0.0\n"
    "overrides:\n"
    "  patch_states:\n"
    "    - id: start\n"
    "      description: tweaked start step\n"
)


def _run_init(tmp_path: Path):
    runner = CliRunner()
    return runner.invoke(
        cli,
        [
            "--project", str(tmp_path),
            "init", "--dev", "--turnstile-dir", str(REPO_ROOT),
            "--enforce", "monitor",
        ],
    )


def _read_registry(tmp_path: Path) -> dict:
    return yaml.safe_load(
        (tmp_path / ".processes" / "registry.yaml").read_text()
    )


def test_init_registers_preexisting_definition_in_registry(tmp_path):
    """A definition present before init lands in registry.local alongside
    the starters, so non-empty-local discovery can still find it."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "custom-proc.yaml").write_text(CUSTOM_DEF)

    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output

    registry = _read_registry(tmp_path)
    local = registry["local"]
    assert "custom-proc" in local, (
        "pre-existing definition missing from registry.local; it is "
        "unstartable because auto-discovery is off when local is non-empty"
    )
    assert STARTER_STEMS <= set(local), "starters must still be registered"


def test_init_makes_preexisting_definition_startable(tmp_path):
    """After init, `turnstile list` shows the pre-existing process (the
    quickstart flow: create feature-deploy.yaml, run init, start it)."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "custom-proc.yaml").write_text(CUSTOM_DEF)

    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output

    runner = CliRunner()
    listed = runner.invoke(cli, ["--project", str(tmp_path), "list"])
    assert listed.exit_code == 0, listed.output
    assert "custom-proc" in listed.output


def test_init_output_mentions_registered_preexisting_definition(tmp_path):
    """Init tells the user which pre-existing definitions it registered."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "custom-proc.yaml").write_text(CUSTOM_DEF)

    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output
    assert "custom-proc" in result.output


def test_init_registers_preexisting_override_file_so_it_resolves(tmp_path):
    """A pre-existing top-level override file is registered in local:
    alongside its parent, and the derived process is discoverable.

    Target mutant: excluding override stems from local (the earlier
    spec). A non-empty registry.local disables the lenient auto-discovery
    that used to resolve top-level overrides, so an excluded override
    becomes silently unusable: `list` omits the derived process with no
    error anywhere.
    """
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "custom-proc.yaml").write_text(CUSTOM_DEF)
    (proc_dir / "tweaked-proc.yaml").write_text(OVERRIDE_DEF)

    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output

    local = _read_registry(tmp_path)["local"]
    assert "tweaked-proc" in local
    assert "custom-proc" in local, "parent must be registered too"

    runner = CliRunner()
    listed = runner.invoke(cli, ["--project", str(tmp_path), "list"])
    assert listed.exit_code == 0, listed.output
    assert "tweaked-proc" in listed.output, (
        "derived process must be discoverable after init"
    )
    assert "custom-proc" in listed.output


def test_init_skips_override_with_unresolvable_extends_target(tmp_path):
    """A pre-existing override extending a process that will not exist
    after init (not a starter, no matching pre-existing definition) is
    skipped with a stderr warning, and the project stays usable.

    Target mutant: parse-check-only registration. load_override succeeds
    on the dangling override, so registering its stem puts a strictly
    resolved entry with a missing parent into registry.local; discovery
    then raises on it, and every subsequent command in the project dies.
    """
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "dangling-proc.yaml").write_text(
        OVERRIDE_DEF.replace("custom-proc", "long-gone-proc").replace(
            "tweaked-proc", "dangling-proc"
        )
    )

    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output

    assert "dangling-proc" not in _read_registry(tmp_path)["local"]
    assert "dangling-proc.yaml" in result.stderr, (
        f"warning must name the file; stderr was: {result.stderr!r}"
    )
    assert "long-gone-proc" in result.stderr, (
        f"warning must name the missing extends target; stderr was: "
        f"{result.stderr!r}"
    )
    assert "not registered" in result.stderr

    runner = CliRunner()
    listed = runner.invoke(cli, ["--project", str(tmp_path), "list"])
    assert listed.exit_code == 0, (
        f"project must stay usable after init; output: {listed.output}"
    )


def test_init_registers_override_extending_a_starter(tmp_path):
    """A pre-existing override whose extends target is a starter (which
    init is about to install) is registered and resolves.

    Target mutant: checking the extends target only against pre-existing
    files. The starter parent does not exist yet when init scans, so that
    check would silently drop a valid override of a starter process.
    """
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "tweaked-proc.yaml").write_text(
        OVERRIDE_DEF.replace("custom-proc", "peer-review").replace(
            "id: start", "id: prepare"
        )
    )

    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output

    assert "tweaked-proc" in _read_registry(tmp_path)["local"]

    runner = CliRunner()
    listed = runner.invoke(cli, ["--project", str(tmp_path), "list"])
    assert listed.exit_code == 0, listed.output
    assert "tweaked-proc" in listed.output


def test_init_quotes_hostile_filename_stem_in_registry(tmp_path):
    """A pre-existing definition in a file whose stem contains YAML
    metacharacters (colon + space) is registered as a plain string and
    the registry stays parseable.

    Target mutant: interpolating the raw stem into the registry template
    would emit `- weird: name`, which YAML parses as a mapping entry;
    RegistryConfig requires local to be a list of strings, so every
    subsequent command in the project would fail at registry load.
    """
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "weird: name.yaml").write_text(
        CUSTOM_DEF.replace("custom-proc", "weird-proc")
    )

    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output

    registry = _read_registry(tmp_path)  # must parse as valid YAML
    assert "weird: name" in registry["local"], (
        f"stem must survive as a string entry; local was: "
        f"{registry['local']!r}"
    )

    runner = CliRunner()
    listed = runner.invoke(cli, ["--project", str(tmp_path), "list"])
    assert listed.exit_code == 0, listed.output
    assert "weird-proc" in listed.output


def test_init_does_not_duplicate_starter_name_collisions(tmp_path):
    """A pre-existing file that shares a starter's filename is already
    covered by the starter registry entry; it must not appear twice."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    custom = CUSTOM_DEF.replace("custom-proc", "my-custom-bug-fix")
    (proc_dir / "bug-fix.yaml").write_text(custom)

    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output

    local = _read_registry(tmp_path)["local"]
    assert local.count("bug-fix") == 1
    assert len(local) == len(set(local)), f"duplicate entries: {local}"


def test_init_warns_and_skips_unparseable_preexisting_file(tmp_path):
    """A pre-existing file with broken YAML syntax is skipped with a
    stderr warning; valid siblings are still registered and init exits 0.

    Target mutants: (a) registering the broken stem would make every
    subsequent discovery fail hard, since the loader raises on files
    listed in registry.local; (b) crashing init on the broken file would
    abort setup entirely; (c) skipping it silently would hide from the
    user that their process was not registered.
    """
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "broken.yaml").write_text("name: broken\nstates: [unclosed\n")
    (proc_dir / "custom-proc.yaml").write_text(CUSTOM_DEF)

    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output

    local = _read_registry(tmp_path)["local"]
    assert "broken" not in local
    assert "custom-proc" in local, "valid sibling must still be registered"

    assert "broken.yaml" in result.stderr, "warning must name the file"
    assert "not registered" in result.stderr

    # Discovery must remain healthy after init (the hard-fail mutant).
    runner = CliRunner()
    listed = runner.invoke(cli, ["--project", str(tmp_path), "list"])
    assert listed.exit_code == 0, listed.output
    assert "custom-proc" in listed.output


def test_init_warns_and_skips_non_definition_yaml_file(tmp_path):
    """A pre-existing file that parses as YAML but is not a process
    definition (stray config, notes) is skipped with a stderr warning.

    Target mutant: registering any *.yaml stem without checking it loads
    as a definition would poison registry.local with stray files and
    break discovery for the whole project.
    """
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "notes.yaml").write_text(
        "just_some_config: true\nvalues:\n  - a\n  - b\n"
    )
    (proc_dir / "custom-proc.yaml").write_text(CUSTOM_DEF)

    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output

    local = _read_registry(tmp_path)["local"]
    assert "notes" not in local
    assert "custom-proc" in local, "valid sibling must still be registered"

    assert "notes.yaml" in result.stderr, "warning must name the file"
    assert "not registered" in result.stderr

    runner = CliRunner()
    listed = runner.invoke(cli, ["--project", str(tmp_path), "list"])
    assert listed.exit_code == 0, listed.output
    assert "custom-proc" in listed.output


def test_init_clean_dir_registry_is_starters_only(tmp_path):
    """Guard-rail: with no pre-existing files, the generated registry has
    exactly the starter content (same local set, same settings)."""
    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output

    expected = yaml.safe_load(
        STARTER_REGISTRY.replace("__ENFORCEMENT__", "monitor")
    )
    actual = _read_registry(tmp_path)
    assert set(actual["local"]) == set(expected["local"])
    assert len(actual["local"]) == len(expected["local"])
    assert actual["settings"] == expected["settings"]
    assert actual.get("extends", []) == expected.get("extends", [])
