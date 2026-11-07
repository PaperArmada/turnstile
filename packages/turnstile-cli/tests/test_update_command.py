"""`turnstile update` upgrades starters safely (GH #47).

The command used to clone the repo's default branch and overwrite every
definition whose content differed, including copies a project had edited,
and it left the release pinned in the generated configs untouched. It now
compares against the starters bundled with the installed CLI, replaces only
unedited copies (content recorded in .processes/.starters.lock or shipped by
an earlier release), keeps edited and locally newer copies unless forced,
refuses to swap a definition out from under an active instance whose state
the new version removes, and re-pins .mcp.json and .claude/settings.json.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from turnstile_cli import upgrade
from turnstile_cli.claude_adapter import TURNSTILE_REF
from turnstile_cli.main import cli
from turnstile_cli.starters import STARTERS
from turnstile_core.persistence import StateStore

REPO_ROOT = Path(__file__).resolve().parents[3]


def _definition(name: str, version: str, states=("start", "work", "done")) -> str:
    first, *middle, last = states
    body = [
        f"name: {name}",
        f'version: "{version}"',
        "states:",
        f"  - id: {first}",
        "    type: initial",
        f"    transitions: [{middle[0] if middle else last}]",
    ]
    for i, state in enumerate(middle):
        nxt = middle[i + 1] if i + 1 < len(middle) else last
        body += [f"  - id: {state}", f"    transitions: [{nxt}]"]
    body += [f"  - id: {last}", "    type: terminal", ""]
    return "\n".join(body)


OLD = _definition("demo", "1.0.0")
NEW = _definition("demo", "1.1.0")
BUNDLE = {"demo.yaml": NEW}


@pytest.fixture
def proc_dir(tmp_path):
    d = tmp_path / ".processes"
    d.mkdir()
    return d


def _install(proc_dir: Path, content: str = OLD, record: bool = True) -> Path:
    path = proc_dir / "demo.yaml"
    path.write_text(content)
    if record:
        upgrade.write_manifest(
            proc_dir,
            {"demo.yaml": {"release": "v0.0.1", "sha256": upgrade.content_hash(content)}},
        )
    return path


def _status(plan: upgrade.Plan, name: str = "demo") -> str:
    return next(e.status for e in plan.entries if e.name == name)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_unedited_copy_recorded_in_the_manifest_is_updated(proc_dir):
    _install(proc_dir)
    assert _status(upgrade.plan_update(proc_dir, BUNDLE, [])) == upgrade.APPLY


def test_identical_copy_is_up_to_date(proc_dir):
    _install(proc_dir, NEW)
    assert _status(upgrade.plan_update(proc_dir, BUNDLE, [])) == upgrade.UP_TO_DATE


def test_edited_copy_is_kept(proc_dir):
    _install(proc_dir)
    (proc_dir / "demo.yaml").write_text(OLD + "# local tweak\n")
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert _status(plan) == upgrade.EDITED
    assert [e.name for e in plan.skipped] == ["demo"]
    assert "# local tweak" in upgrade.diff(plan.entries[0])


def test_copy_without_a_manifest_is_edited_unless_a_release_shipped_it(
    proc_dir, monkeypatch
):
    _install(proc_dir, record=False)
    assert _status(upgrade.plan_update(proc_dir, BUNDLE, [])) == upgrade.EDITED

    monkeypatch.setitem(
        upgrade.RELEASED_STARTER_HASHES, "demo.yaml", [upgrade.content_hash(OLD)]
    )
    assert _status(upgrade.plan_update(proc_dir, BUNDLE, [])) == upgrade.APPLY


def test_crlf_checkout_of_an_unedited_copy_counts_as_unedited(proc_dir):
    _install(proc_dir)
    (proc_dir / "demo.yaml").write_bytes(OLD.replace("\n", "\r\n").encode())
    assert _status(upgrade.plan_update(proc_dir, BUNDLE, [])) == upgrade.APPLY


def test_unedited_copy_newer_than_the_source_is_kept(proc_dir):
    _install(proc_dir, _definition("demo", "2.0.0"))
    assert _status(upgrade.plan_update(proc_dir, BUNDLE, [])) == upgrade.LOCAL_NEWER


def test_deleted_starter_is_not_reinstalled(proc_dir):
    _install(proc_dir)
    (proc_dir / "demo.yaml").unlink()
    assert _status(upgrade.plan_update(proc_dir, BUNDLE, [])) == upgrade.REMOVED


def test_starter_never_installed_is_new(proc_dir):
    assert _status(upgrade.plan_update(proc_dir, BUNDLE, [])) == upgrade.NEW


def test_local_definition_absent_from_the_source_is_local_only(proc_dir):
    (proc_dir / "mine.yaml").write_text(_definition("mine", "1.0.0"))
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert _status(plan, "mine") == upgrade.LOCAL_ONLY


def test_unreadable_manifest_warns_and_is_ignored(proc_dir):
    _install(proc_dir)
    (proc_dir / upgrade.MANIFEST_NAME).write_text("{not json")
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert _status(plan) == upgrade.EDITED
    assert any("unreadable" in w for w in plan.warnings)


def _instance(tmp_path: Path, state: str):
    store = StateStore(tmp_path / ".process-state")
    instance = store.create(process_name="demo", initial_state="start")
    instance.current_state = state
    store.save(instance)
    return store.load(instance.instance_id)


def test_instance_in_a_state_the_new_version_removes_blocks_the_update(
    proc_dir, tmp_path
):
    _install(proc_dir)
    bundle = {"demo.yaml": _definition("demo", "1.1.0", ("start", "build", "done"))}
    instance = _instance(tmp_path, "work")
    plan = upgrade.plan_update(proc_dir, bundle, [instance])
    assert _status(plan) == upgrade.BLOCKED
    assert instance.instance_id in plan.entries[0].detail


def test_instance_in_a_state_the_new_version_keeps_does_not_block(
    proc_dir, tmp_path
):
    _install(proc_dir)
    instance = _instance(tmp_path, "work")
    plan = upgrade.plan_update(proc_dir, BUNDLE, [instance])
    assert _status(plan) == upgrade.APPLY
    assert plan.entries[0].affected == [
        instance.instance_id
    ]


@pytest.mark.parametrize("case", ["edited", "local-newer", "blocked"])
def test_force_replaces_a_kept_definition(proc_dir, tmp_path, case):
    bundle = BUNDLE
    instances = []
    if case == "edited":
        _install(proc_dir, OLD + "# local tweak\n", record=False)
    elif case == "local-newer":
        _install(proc_dir, _definition("demo", "2.0.0"))
    else:
        _install(proc_dir)
        bundle = {"demo.yaml": _definition("demo", "1.1.0", ("start", "build", "done"))}
        instances = [_instance(tmp_path, "work")]
    plan = upgrade.plan_update(proc_dir, bundle, instances, frozenset({"demo"}))
    entry = plan.entries[0]
    assert (entry.status, entry.forced) == (upgrade.APPLY, True)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def test_apply_writes_only_planned_files_and_records_them(proc_dir):
    _install(proc_dir)
    (proc_dir / "kept.yaml").write_text(_definition("kept", "1.0.0") + "# edit\n")
    bundle = {**BUNDLE, "kept.yaml": _definition("kept", "1.1.0")}
    plan = upgrade.plan_update(proc_dir, bundle, [])
    written = upgrade.apply_plan(proc_dir, plan, "v9.9.9")

    assert [e.name for e in written] == ["demo"]
    assert (proc_dir / "demo.yaml").read_text() == NEW
    assert (proc_dir / "kept.yaml").read_text().endswith("# edit\n")
    manifest, _ = upgrade.load_manifest(proc_dir)
    assert manifest["demo.yaml"] == {
        "release": "v9.9.9",
        "sha256": upgrade.content_hash(NEW),
    }
    assert "kept.yaml" not in manifest


def test_apply_refreshes_a_stale_entry_for_a_file_that_equals_the_source(proc_dir):
    _install(proc_dir)  # manifest records OLD's hash
    (proc_dir / "demo.yaml").write_text(NEW)  # updated by hand
    upgrade.apply_plan(proc_dir, upgrade.plan_update(proc_dir, BUNDLE, []), "v9.9.9")
    manifest, _ = upgrade.load_manifest(proc_dir)
    assert manifest["demo.yaml"]["sha256"] == upgrade.content_hash(NEW)


def test_new_starter_is_registered_when_registry_lists_local_processes(proc_dir):
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nlocal:\n- other\n")
    registered, warning = upgrade.register_new(proc_dir, ["demo"])
    assert (registered, warning) == (["demo"], "")
    assert yaml.safe_load((proc_dir / "registry.yaml").read_text())["local"] == [
        "demo",
        "other",
    ]


def test_new_starter_needs_no_registration_when_registry_auto_discovers(proc_dir):
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nlocal: []\n")
    assert upgrade.register_new(proc_dir, ["demo"]) == ([], "")


def _spec(ref: str, url: str = "https://github.com/PaperArmada/turnstile.git") -> str:
    return f"turnstile-mcp @ git+{url}@{ref}#subdirectory=packages/turnstile-mcp"


def _mcp(tmp_path: Path, spec: str) -> Path:
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"args": ["--from", spec]}))
    return path


def test_repin_moves_an_older_release_tag_and_leaves_dev_configs_alone(tmp_path):
    mcp = _mcp(tmp_path, _spec("v0.1.5"))
    (tmp_path / ".claude").mkdir()
    dev = {"hooks": {"command": "uv --directory /src run turnstile guard"}}
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(dev))

    changes = upgrade.repin(tmp_path, "v9.9.9")

    assert [(c.path, c.old, c.action) for c in changes] == [
        (".mcp.json", "v0.1.5", "repin")
    ]
    assert upgrade.find_pins(mcp.read_text()) == {"v9.9.9"}
    assert json.loads((tmp_path / ".claude" / "settings.json").read_text()) == dev


@pytest.mark.parametrize(
    "spec,reason",
    [
        (_spec("v0.1.7"), "newer"),
        (_spec("main"), "not a release tag"),
        (_spec("2e5d2af"), "not a release tag"),
        (_spec("v0.1.5", "https://github.com/me/turnstile.git"), "re-pins only"),
    ],
    ids=["newer-tag", "branch", "commit", "fork"],
)
def test_repin_keeps_pins_it_cannot_safely_move(tmp_path, spec, reason):
    mcp = _mcp(tmp_path, spec)
    before = mcp.read_text()
    changes = upgrade.repin(tmp_path, "v0.1.6")
    assert [c.action for c in changes] == ["keep"]
    assert reason in changes[0].reason
    assert mcp.read_text() == before


# ---------------------------------------------------------------------------
# Files in the way, duplicates, registry edits, overrides, extends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "occupant",
    [_definition("ship-it", "3.0.0"), "name: [unclosed\n", "extends: x\noverrides: {}\n"],
    ids=["other-name", "unparsable", "unnamed-override"],
)
def test_new_starter_never_overwrites_a_file_at_its_path(proc_dir, occupant):
    (proc_dir / "demo.yaml").write_text(occupant)
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert _status(plan) == upgrade.CONFLICT
    upgrade.apply_plan(proc_dir, plan, "v9.9.9")
    assert (proc_dir / "demo.yaml").read_text() == occupant

    forced = upgrade.plan_update(proc_dir, BUNDLE, [], frozenset({"demo"}))
    upgrade.apply_plan(proc_dir, forced, "v9.9.9")
    assert (proc_dir / "demo.yaml").read_text() == NEW


def test_force_reinstalls_a_deleted_starter(proc_dir):
    _install(proc_dir)
    (proc_dir / "demo.yaml").unlink()
    plan = upgrade.plan_update(proc_dir, BUNDLE, [], frozenset({"demo"}))
    assert (_status(plan), plan.entries[0].forced) == (upgrade.NEW, True)


@pytest.mark.parametrize(
    "files,local,used",
    [
        (["demo.yaml", "zz-demo.yaml"], ["demo"], "demo.yaml"),
        (["a.yaml", "b.yaml"], ["b", "a"], "a.yaml"),
    ],
    ids=["listed-sorts-first", "last-in-registry-order"],
)
def test_duplicate_names_compare_the_file_the_loader_uses(
    proc_dir, monkeypatch, files, local, used
):
    monkeypatch.setitem(
        upgrade.RELEASED_STARTER_HASHES, "demo.yaml", [upgrade.content_hash(OLD)]
    )
    for name in files:
        (proc_dir / name).write_text(OLD)
    listing = "".join(f"- {stem}\n" for stem in local)
    (proc_dir / "registry.yaml").write_text(f"version: '1.0'\nlocal:\n{listing}")
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert plan.entries[0].local_path == proc_dir / used
    assert any(f"comparing {used}" in w for w in plan.warnings)


@pytest.mark.parametrize("indent", ["", "  ", "    "])
def test_register_new_keeps_the_list_indentation(proc_dir, indent):
    registry = proc_dir / "registry.yaml"
    registry.write_text(f"version: '1.0'\nlocal:\n{indent}- other\nsettings: {{}}\n")
    registered, warning = upgrade.register_new(proc_dir, ["demo"])
    assert (registered, warning) == (["demo"], "")
    assert yaml.safe_load(registry.read_text())["local"] == ["demo", "other"]


def test_register_new_refuses_a_form_it_cannot_extend(proc_dir):
    registry = proc_dir / "registry.yaml"
    registry.write_text("version: '1.0'\nlocal: [other]\n")
    registered, warning = upgrade.register_new(proc_dir, ["demo"])
    assert registered == [] and "by hand" in warning
    assert registry.read_text() == "version: '1.0'\nlocal: [other]\n"


PARENT_OLD = _definition("parent", "1.0.0", ("start", "work", "done"))
PARENT_NEW = _definition("parent", "1.1.0", ("start", "build", "done"))
CHILD = "name: child\nextends: parent\noverrides: {}\n"


def _instance_of(tmp_path: Path, name: str, state: str):
    store = StateStore(tmp_path / ".process-state")
    instance = store.create(process_name=name, initial_state="start")
    instance.current_state = state
    store.save(instance)
    return store.load(instance.instance_id)


def test_override_starter_instance_in_an_inherited_state_does_not_block(
    proc_dir, tmp_path
):
    (proc_dir / "parent.yaml").write_text(PARENT_OLD)
    (proc_dir / "child.yaml").write_text(CHILD)
    upgrade.write_manifest(proc_dir, {
        "child.yaml": {"release": "v0", "sha256": upgrade.content_hash(CHILD)},
    })
    child_new = CHILD + "# v2\n"
    bundle = {"parent.yaml": PARENT_OLD, "child.yaml": child_new}
    plan = upgrade.plan_update(proc_dir, bundle, [_instance_of(tmp_path, "child", "work")])
    assert _status(plan, "child") == upgrade.APPLY


def test_parent_update_is_blocked_by_an_instance_of_a_definition_extending_it(
    proc_dir, tmp_path
):
    (proc_dir / "parent.yaml").write_text(PARENT_OLD)
    (proc_dir / "child.yaml").write_text(CHILD)
    upgrade.write_manifest(proc_dir, {
        "parent.yaml": {"release": "v0", "sha256": upgrade.content_hash(PARENT_OLD)},
    })
    instance = _instance_of(tmp_path, "child", "work")
    plan = upgrade.plan_update(proc_dir, {"parent.yaml": PARENT_NEW}, [instance])
    assert _status(plan, "parent") == upgrade.BLOCKED
    assert instance.instance_id in plan.entries[0].detail


def test_starter_supplied_by_an_extended_source_is_not_copied(tmp_path, proc_dir):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "demo.yaml").write_text(OLD)
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nextends:\n- source: ./shared\n"
    )
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert _status(plan) == upgrade.EXTERNAL
    assert plan.entries[0].detail == "provided by local:./shared"
    assert plan.writes == []


def test_repin_message_never_prints_url_credentials(tmp_path):
    _mcp(tmp_path, _spec("v0.1.5", "https://user:s3cret@github.com/me/turnstile.git"))
    (change,) = upgrade.plan_repins(tmp_path, "v0.1.6")
    assert "s3cret" not in change.reason


# ---------------------------------------------------------------------------
# The plan is checked with the loader (second review of #47)
# ---------------------------------------------------------------------------

P1 = _definition("parent", "1.0.0", ("start", "work", "done"))
P2 = _definition("parent", "1.1.0", ("start", "build", "done"))
C1 = "name: child\nextends: parent\noverrides:\n  patch_states:\n  - id: work\n    description: x\n"
C2 = C1.replace("id: work", "id: build")


def _record(proc_dir: Path, **files: str) -> None:
    for name, text in files.items():
        (proc_dir / name).write_text(text)
    upgrade.write_manifest(proc_dir, {
        n: {"release": "v0", "sha256": upgrade.content_hash(t)} for n, t in files.items()
    })


def _loads(proc_dir: Path) -> bool:
    from turnstile_core.loader import discover_definitions
    discover_definitions(proc_dir.parent)
    return True


def test_parent_and_child_updated_together_both_apply(proc_dir, tmp_path):
    _record(proc_dir, **{"parent.yaml": P1, "child.yaml": C1})
    bundle = {"parent.yaml": P2, "child.yaml": C2}
    plan = upgrade.plan_update(proc_dir, bundle, [_instance_of(tmp_path, "child", "start")])
    assert (_status(plan, "parent"), _status(plan, "child")) == (upgrade.APPLY, upgrade.APPLY)
    upgrade.apply_plan(proc_dir, plan, "v9")
    assert _loads(proc_dir)


def test_child_is_held_back_when_its_kept_parent_cannot_take_it(proc_dir):
    _record(proc_dir, **{"child.yaml": C1})
    (proc_dir / "parent.yaml").write_text(P1 + "# edited\n")
    plan = upgrade.plan_update(proc_dir, {"parent.yaml": P2, "child.yaml": C2}, [])
    assert _status(plan, "parent") == upgrade.EDITED
    assert _status(plan, "child") == upgrade.BLOCKED
    assert "unloadable" in next(e for e in plan.entries if e.name == "child").detail
    upgrade.apply_plan(proc_dir, plan, "v9")
    assert _loads(proc_dir)


def test_parent_is_held_back_when_a_local_override_would_stop_loading(proc_dir):
    _record(proc_dir, **{"parent.yaml": P1})
    (proc_dir / "mine.yaml").write_text(C1.replace("name: child", "name: mine"))
    plan = upgrade.plan_update(proc_dir, {"parent.yaml": P2}, [])
    assert _status(plan, "parent") == upgrade.BLOCKED
    upgrade.apply_plan(proc_dir, plan, "v9")
    assert _loads(proc_dir)


def test_force_never_writes_an_unloadable_plan(proc_dir):
    _record(proc_dir, **{"parent.yaml": P1})
    (proc_dir / "mine.yaml").write_text(C1.replace("name: child", "name: mine"))
    plan = upgrade.plan_update(
        proc_dir, {"parent.yaml": P2}, [], frozenset({"parent"})
    )
    assert _status(plan, "parent") == upgrade.BLOCKED


@pytest.mark.parametrize("where", ["overrides/kid.yaml", "kid.yaml"])
@pytest.mark.parametrize("extends", ["parent", "'@acme/pkg/parent'"])
def test_instance_of_any_definition_extending_the_parent_blocks_it(
    proc_dir, tmp_path, where, extends
):
    _record(proc_dir, **{"parent.yaml": P1})
    kid = proc_dir / where
    kid.parent.mkdir(exist_ok=True)
    kid.write_text(f"name: kid\nextends: {extends}\noverrides: {{}}\n")
    instance = _instance_of(tmp_path, "kid", "work")
    plan = upgrade.plan_update(proc_dir, {"parent.yaml": P2}, [instance])
    assert _status(plan, "parent") == upgrade.BLOCKED
    assert instance.instance_id in plan.entries[0].detail


def test_override_reusing_its_parents_name_is_not_compared_as_the_copy(proc_dir):
    _record(proc_dir, **{"demo.yaml": OLD})
    (proc_dir / "zz-demo.yaml").write_text(
        "name: demo\nextends: demo\noverrides: {}\n"
    )
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    entry = plan.entries[0]
    assert (entry.status, entry.local_path) == (upgrade.APPLY, proc_dir / "demo.yaml")


def test_override_starter_is_compared_with_its_local_override_copy(proc_dir):
    _record(proc_dir, **{"parent.yaml": P1, "child.yaml": C1})
    bundle = {"parent.yaml": P1, "child.yaml": C1 + "# v2\n"}
    plan = upgrade.plan_update(proc_dir, bundle, [])
    assert _status(plan, "child") == upgrade.APPLY


def test_directory_at_the_starter_path_is_never_written(proc_dir):
    (proc_dir / "demo.yaml").mkdir()
    plan = upgrade.plan_update(proc_dir, BUNDLE, [], frozenset({"demo"}))
    assert _status(plan) == upgrade.CONFLICT
    upgrade.apply_plan(proc_dir, plan, "v9")
    assert (proc_dir / "demo.yaml").is_dir()


def test_non_utf8_definition_does_not_crash_the_plan(proc_dir):
    (proc_dir / "junk.yaml").write_bytes(b"name: \xff\xfe\n")
    upgrade.plan_update(proc_dir, BUNDLE, [])


def test_remote_extends_source_is_not_fetched_and_new_starters_wait(proc_dir, tmp_path):
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nextends:\n- source: git+file:///nonexistent/repo.git#v1\n"
    )
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert _status(plan) == upgrade.UNVERIFIED
    assert not (tmp_path / ".process-state").exists()
    forced = upgrade.plan_update(proc_dir, BUNDLE, [], frozenset({"demo"}))
    assert _status(forced) == upgrade.NEW


def test_forced_conflict_is_registered(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nlocal:\n- other\n")
    (proc_dir / "other.yaml").write_text(_definition("other", "1.0.0"))
    (proc_dir / "demo.yaml").write_text("name: [unclosed\n")
    source = tmp_path / "src"
    (source / ".processes").mkdir(parents=True)
    (source / ".processes" / "demo.yaml").write_text(NEW)
    result = _run(
        tmp_path, "update", "--apply", "--force", "demo",
        "--dev", "--turnstile-dir", str(source),
    )
    assert result.exit_code == 0, result.output
    assert "demo" in yaml.safe_load((proc_dir / "registry.yaml").read_text())["local"]


# ---------------------------------------------------------------------------
# The check covers registration, empty registries, extends sources, and
# attributes each failure to the write that causes it (third review of #47)
# ---------------------------------------------------------------------------


def _dev_source(tmp_path: Path, **files: str) -> Path:
    source = tmp_path / "src"
    (source / ".processes").mkdir(parents=True)
    for name, text in files.items():
        (source / ".processes" / name).write_text(text)
    return source


def test_new_starter_that_would_collide_once_registered_is_held_back(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nlocal:\n- parent\n")
    _record(proc_dir, **{"parent.yaml": P1})
    (proc_dir / "overrides").mkdir()
    (proc_dir / "overrides" / "mine.yaml").write_text(
        "name: shiny\nextends: parent\noverrides: {}\n"
    )
    source = _dev_source(
        tmp_path, **{"parent.yaml": P1, "shiny.yaml": _definition("shiny", "1.0.0")}
    )
    result = _run(tmp_path, "update", "--apply", "--dev", "--turnstile-dir", str(source))
    assert result.exit_code == 0, result.output
    assert "would leave the definitions unloadable" in result.output
    assert not (proc_dir / "shiny.yaml").exists()
    assert _loads(proc_dir)


def test_new_override_starter_colliding_with_a_listed_definition_is_held_back(proc_dir):
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nlocal:\n- parent\n- mychild\n")
    _record(proc_dir, **{"parent.yaml": P1})
    (proc_dir / "mychild.yaml").write_text(_definition("child", "0.1.0"))
    plan = upgrade.plan_update(proc_dir, {"parent.yaml": P1, "child.yaml": C1}, [])
    assert _status(plan, "child") == upgrade.BLOCKED


def test_unlisted_starter_is_updated_but_stays_unlisted(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nlocal:\n- parent\n- mine\n")
    _record(proc_dir, **{"parent.yaml": P1, "demo.yaml": OLD})
    # A listed override named like the unlisted starter: listing demo.yaml
    # too would make the two collide.
    (proc_dir / "mine.yaml").write_text("name: demo\nextends: parent\noverrides: {}\n")
    source = _dev_source(tmp_path, **{"parent.yaml": P1, "demo.yaml": NEW})
    result = _run(tmp_path, "update", "--apply", "--dev", "--turnstile-dir", str(source))
    assert result.exit_code == 0, result.output
    assert (proc_dir / "demo.yaml").read_text() == NEW
    listed = yaml.safe_load((proc_dir / "registry.yaml").read_text())["local"]
    assert listed == ["parent", "mine"]
    assert _loads(proc_dir)


@pytest.mark.parametrize("registry", ["# comments only\n", "{}\n", ""])
def test_empty_registry_is_the_default_and_the_check_still_runs(proc_dir, registry):
    (proc_dir / "registry.yaml").write_text(registry)
    _record(proc_dir, **{"child.yaml": C1})
    (proc_dir / "parent.yaml").write_text(P1 + "# edited\n")
    plan = upgrade.plan_update(proc_dir, {"parent.yaml": P2, "child.yaml": C2}, [])
    assert plan.warnings == []
    assert _status(plan, "child") == upgrade.BLOCKED
    upgrade.apply_plan(proc_dir, plan, "v9")
    assert _loads(proc_dir)


def _remote_registry(proc_dir: Path, url: str = "file:///nowhere/remote.git") -> str:
    (proc_dir / "registry.yaml").write_text(
        f"version: '1.0'\nextends:\n- source: git+{url}#v1\n"
    )
    (proc_dir / "overrides").mkdir()
    (proc_dir / "overrides" / "r.yaml").write_text(
        "name: myremote\nextends: remoteproc\noverrides: {}\n"
    )
    return url


def test_override_of_a_git_provided_parent_is_checked_against_the_cache(proc_dir):
    url = _remote_registry(proc_dir)
    # Where the loader keeps its last fetch of the source.
    cached = (
        proc_dir.parent / ".process-state" / ".cache" / "git"
        / url.replace("://", "_").replace("/", "_").replace(".", "_") / "processes"
    )
    cached.mkdir(parents=True)
    (cached / "remoteproc.yaml").write_text(_definition("remoteproc", "1.0.0"))
    (cached / "fresh.yaml").write_text(_definition("fresh", "1.0.0"))
    _record(proc_dir, **{"child.yaml": C1})
    (proc_dir / "parent.yaml").write_text(P1 + "# edited\n")
    bundle = {
        "parent.yaml": P2, "child.yaml": C2, "fresh.yaml": _definition("fresh", "2.0.0"),
    }
    plan = upgrade.plan_update(proc_dir, bundle, [])
    assert [w.split(" as of")[0] for w in plan.warnings] == [f"checked against git+{url}#v1"]
    assert plan.unsafe == ""
    assert _status(plan, "child") == upgrade.BLOCKED
    fresh = next(e for e in plan.entries if e.name == "fresh")
    assert fresh.status == upgrade.EXTERNAL
    assert fresh.detail == f"provided by git:git+{url}#v1"


def test_a_python_source_that_fails_to_resolve_does_not_crash_the_plan(proc_dir):
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nextends:\n- source: sys\n")
    _record(proc_dir, **{"demo.yaml": OLD})
    assert _status(upgrade.plan_update(proc_dir, BUNDLE, [])) == upgrade.APPLY


def test_python_package_source_is_read_without_fetching(proc_dir, tmp_path, monkeypatch):
    package = tmp_path / "site" / "acme_processes"
    (package / "processes").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "processes" / "demo.yaml").write_text(OLD)
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nextends:\n- source: pypi:acme-processes\n"
    )
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert _status(plan) == upgrade.EXTERNAL
    assert plan.entries[0].detail == "provided by python:pypi:acme-processes"


def test_two_independent_breakages_hold_back_only_their_writes(proc_dir):
    def renamed(text: str) -> str:
        return text.replace("child", "child2").replace("parent", "parent2")

    _record(proc_dir, **{"child.yaml": C1, "child2.yaml": renamed(C1), "demo.yaml": OLD})
    (proc_dir / "parent.yaml").write_text(P1 + "# edited\n")
    (proc_dir / "parent2.yaml").write_text(renamed(P1) + "# edited\n")
    bundle = {
        "parent.yaml": P2, "child.yaml": C2,
        "parent2.yaml": renamed(P2), "child2.yaml": renamed(C2), "demo.yaml": NEW,
    }
    plan = upgrade.plan_update(proc_dir, bundle, [])
    assert plan.unsafe == ""
    assert (_status(plan, "child"), _status(plan, "child2"), _status(plan)) == (
        upgrade.BLOCKED, upgrade.BLOCKED, upgrade.APPLY,
    )
    upgrade.apply_plan(proc_dir, plan, "v9")
    assert _loads(proc_dir)


def test_only_the_write_that_strands_an_instance_is_held_back(proc_dir, tmp_path):
    child_v1 = (
        "name: child\nextends: parent\nversion: '1.0.0'\noverrides:\n"
        "  add_states:\n  - id: extra\n    transitions: [done]\n"
    )
    child_v2 = "name: child\nextends: parent\nversion: '1.1.0'\noverrides: {}\n"
    _record(proc_dir, **{"parent.yaml": P1, "child.yaml": child_v1})
    instance = _instance_of(tmp_path, "child", "extra")
    bundle = {"parent.yaml": P1.replace("1.0.0", "1.0.1"), "child.yaml": child_v2}
    plan = upgrade.plan_update(proc_dir, bundle, [instance])
    assert (_status(plan, "parent"), _status(plan, "child")) == (
        upgrade.APPLY, upgrade.BLOCKED,
    )
    assert instance.instance_id in next(e for e in plan.entries if e.name == "child").detail

    forced = upgrade.plan_update(proc_dir, bundle, [instance], frozenset({"child"}))
    child = next(e for e in forced.entries if e.name == "child")
    assert (_status(forced, "parent"), child.status) == (upgrade.APPLY, upgrade.APPLY)
    assert child.stranded == [f"{instance.instance_id} (child) at 'extra'"]
    # The parent write does not keep the instance running either.
    assert next(e for e in forced.entries if e.name == "parent").affected == []


def test_forced_write_over_another_definition_reports_its_stranded_instance(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "other.yaml").write_text(_definition("other", "1.0.0"))
    instance = _instance_of(tmp_path, "other", "work")
    plan = upgrade.plan_update(proc_dir, {"other.yaml": NEW}, [instance])
    assert _status(plan) == upgrade.CONFLICT
    assert _status(plan, "other") == upgrade.LOCAL_ONLY

    source = _dev_source(tmp_path, **{"other.yaml": NEW})
    result = _run(
        tmp_path, "update", "--apply", "--force", "demo",
        "--dev", "--turnstile-dir", str(source),
    )
    assert result.exit_code == 0, result.output
    assert f"left without their definition or state: {instance.instance_id}" in result.output
    assert "local only" not in result.output


@pytest.mark.parametrize(
    "local", ["local: [parent, child]\n", "local:  # listed\n- parent\n- child\n"]
)
def test_new_starter_the_registry_edit_cannot_list_is_not_relied_on(tmp_path, local):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "registry.yaml").write_text("version: '1.0'\n" + local)
    _record(proc_dir, **{"parent.yaml": P1, "child.yaml": C1})
    child_on_base = C1.replace("extends: parent", "extends: base")
    source = _dev_source(tmp_path, **{
        "parent.yaml": P1, "child.yaml": child_on_base,
        "base.yaml": P1.replace("parent", "base"),
    })
    result = _run(tmp_path, "update", "--apply", "--dev", "--turnstile-dir", str(source))
    assert result.exit_code == 0, result.output
    assert "cannot extend; add base by hand" in result.output
    assert (proc_dir / "child.yaml").read_text() == C1
    assert _loads(proc_dir)


def test_new_starter_a_linked_registry_cannot_list_is_not_relied_on(proc_dir, tmp_path):
    shared = tmp_path / "shared-registry.yaml"
    shared.write_text("version: '1.0'\nlocal:\n- parent\n- child\n")
    (proc_dir / "registry.yaml").symlink_to(shared)
    _record(proc_dir, **{"parent.yaml": P1, "child.yaml": C1})
    bundle = {
        "parent.yaml": P1, "child.yaml": C1.replace("extends: parent", "extends: base"),
        "base.yaml": P1.replace("parent", "base"),
    }
    plan = upgrade.plan_update(proc_dir, bundle, [])
    assert _status(plan, "child") == upgrade.BLOCKED


def test_local_entry_in_a_subdirectory_is_part_of_the_check(proc_dir):
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nlocal:\n- demo\n- team/mine\n")
    (proc_dir / "team").mkdir()
    (proc_dir / "team" / "mine.yaml").write_text(_definition("mine", "1.0.0"))
    _record(proc_dir, **{"demo.yaml": OLD})
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert (plan.warnings, plan.unsafe, _status(plan)) == ([], "", upgrade.APPLY)


def test_extends_entries_naming_one_directory_stay_distinct(tmp_path, proc_dir):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "alpha.yaml").write_text(_definition("alpha", "1.0.0"))
    (shared / "beta.yaml").write_text(_definition("beta", "1.0.0"))
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nextends:\n- source: ./shared\n  processes: [alpha]\n"
        "- source: ./shared/\n  processes: [beta]\n"
    )
    plan = upgrade.plan_update(proc_dir, {"alpha.yaml": _definition("alpha", "2.0.0")}, [])
    assert _status(plan, "alpha") == upgrade.EXTERNAL


def test_pair_that_only_loads_together_applies_beside_other_breakages(proc_dir):
    def renamed(text: str, n: int) -> str:
        return text.replace("child", f"child{n}").replace("parent", f"parent{n}")

    _record(proc_dir, **{
        "parent.yaml": P1, "child.yaml": C1,
        "child2.yaml": renamed(C1, 2), "child3.yaml": renamed(C1, 3),
    })
    for n in (2, 3):
        (proc_dir / f"parent{n}.yaml").write_text(renamed(P1, n) + "# edited\n")
    bundle = {"parent.yaml": P2, "child.yaml": C2}
    for n in (2, 3):
        bundle |= {f"parent{n}.yaml": renamed(P2, n), f"child{n}.yaml": renamed(C2, n)}
    plan = upgrade.plan_update(proc_dir, bundle, [])
    assert [(e.name, e.status) for e in plan.entries if e.name.startswith("child")] == [
        ("child", upgrade.APPLY), ("child2", upgrade.BLOCKED), ("child3", upgrade.BLOCKED),
    ]
    assert _status(plan, "parent") == upgrade.APPLY


def test_pair_that_only_loads_together_applies_beside_one_breakage(proc_dir):
    _record(proc_dir, **{"parent.yaml": P1, "child.yaml": C1, "demo.yaml": OLD})
    (proc_dir / "mine.yaml").write_text("name: mine\nextends: demo\noverrides:\n"
                                        "  patch_states:\n  - id: work\n    description: x\n")
    new_demo = _definition("demo", "1.1.0", ("start", "other", "done"))
    plan = upgrade.plan_update(
        proc_dir, {"parent.yaml": P2, "child.yaml": C2, "demo.yaml": new_demo}, []
    )
    assert (_status(plan, "parent"), _status(plan, "child"), _status(plan)) == (
        upgrade.APPLY, upgrade.APPLY, upgrade.BLOCKED,
    )


def test_writes_that_fix_unloadable_files_are_applied(proc_dir, tmp_path):
    # The recorded parent already moved on; the recorded child still
    # patches the old state, so the files do not load until child updates.
    _record(proc_dir, **{"parent.yaml": P2, "child.yaml": C1})
    stale = _instance_of(tmp_path, "gone", "work")
    plan = upgrade.plan_update(proc_dir, {"parent.yaml": P2, "child.yaml": C2}, [stale])
    assert any("do not load" in w for w in plan.warnings)
    assert (plan.unsafe, _status(plan, "child")) == ("", upgrade.APPLY)
    upgrade.apply_plan(proc_dir, plan, "v9")
    assert _loads(proc_dir)


def test_instance_already_without_its_state_does_not_block(proc_dir, tmp_path):
    _install(proc_dir)
    plan = upgrade.plan_update(proc_dir, BUNDLE, [_instance(tmp_path, "vanished")])
    assert _status(plan) == upgrade.APPLY


def test_instance_stranded_by_a_pair_holds_back_both(proc_dir, tmp_path):
    _record(proc_dir, **{"parent.yaml": P1, "child.yaml": C1})
    instance = _instance_of(tmp_path, "child", "work")
    plan = upgrade.plan_update(proc_dir, {"parent.yaml": P2, "child.yaml": C2}, [instance])
    assert plan.unsafe == ""
    assert (_status(plan, "parent"), _status(plan, "child")) == (
        upgrade.BLOCKED, upgrade.BLOCKED,
    )


def test_blocked_write_reports_no_stranded_instances(proc_dir, tmp_path):
    _record(proc_dir, **{"parent.yaml": P1, "child.yaml": C1})
    instances = [_instance_of(tmp_path, "parent", "work"), _instance_of(tmp_path, "child", "work")]
    plan = upgrade.plan_update(
        proc_dir, {"parent.yaml": P2, "child.yaml": C2}, instances, frozenset({"child"})
    )
    for e in plan.entries:
        if e.status not in upgrade.WRITES:
            assert (e.stranded, e.affected) == ([], []), e.name


def test_linked_overrides_directory_is_never_written_through(proc_dir, tmp_path):
    shared = tmp_path / "shared-overrides"
    shared.mkdir()
    (shared / "child.yaml").write_text(C1)
    (proc_dir / "overrides").symlink_to(shared, target_is_directory=True)
    _record(proc_dir, **{"parent.yaml": P1})
    upgrade.write_manifest(proc_dir, {
        "parent.yaml": {"release": "v0", "sha256": upgrade.content_hash(P1)},
        "child.yaml": {"release": "v0", "sha256": upgrade.content_hash(C1)},
    })
    bundle = {"parent.yaml": P1, "child.yaml": C1 + "# v2\n"}
    plan = upgrade.plan_update(proc_dir, bundle, [], frozenset({"child"}))
    assert _status(plan, "child") == upgrade.CONFLICT
    upgrade.apply_plan(proc_dir, plan, "v9")
    assert (shared / "child.yaml").read_text() == C1


def test_linked_registry_is_never_edited(proc_dir, tmp_path):
    shared = tmp_path / "shared-registry.yaml"
    shared.write_text("version: '1.0'\nlocal:\n- demo\n")
    (proc_dir / "registry.yaml").symlink_to(shared)
    registered, warning = upgrade.register_new(proc_dir, ["fresh"])
    assert registered == [] and "symbolic link" in warning
    assert shared.read_text() == "version: '1.0'\nlocal:\n- demo\n"


def test_symlinked_copy_is_never_written_through(proc_dir, tmp_path):
    shared = tmp_path / "shared-demo.yaml"
    shared.write_text(OLD)
    (proc_dir / "demo.yaml").symlink_to(shared)
    upgrade.write_manifest(
        proc_dir, {"demo.yaml": {"release": "v0", "sha256": upgrade.content_hash(OLD)}}
    )
    plan = upgrade.plan_update(proc_dir, BUNDLE, [], frozenset({"demo"}))
    assert _status(plan) == upgrade.CONFLICT
    upgrade.apply_plan(proc_dir, plan, "v9")
    assert shared.read_text() == OLD


def test_symlink_at_a_new_starters_path_is_never_written_through(proc_dir, tmp_path):
    shared = tmp_path / "shared-other.yaml"
    shared.write_text(_definition("other", "1.0.0"))
    (proc_dir / "demo.yaml").symlink_to(shared)
    plan = upgrade.plan_update(proc_dir, BUNDLE, [], frozenset({"demo"}))
    assert _status(plan) == upgrade.CONFLICT
    upgrade.apply_plan(proc_dir, plan, "v9")
    assert shared.read_text() == _definition("other", "1.0.0")


# ---------------------------------------------------------------------------
# Writes are checked only when every extends source can be read; --apply
# fetches git sources first (sixth review of #47)
# ---------------------------------------------------------------------------


def _git_source(tmp_path: Path, **files: str) -> str:
    repo = tmp_path / "remote"
    repo.mkdir()
    for name, text in files.items():
        (repo / name).write_text(text)
    # A throwaway fixture repo: machine-wide hooks do not apply to it.
    git = [
        "git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
        "-c", "core.hooksPath=/dev/null",
    ]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "chore: fixture"], check=True)
    subprocess.run([*git, "tag", "v1"], check=True)
    return f"git+file://{repo}#v1"


def test_preview_does_not_fetch_and_apply_fetches_then_checks(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    url = _git_source(tmp_path, **{"rparent.yaml": P1.replace("parent", "rparent")})
    (proc_dir / "registry.yaml").write_text(f"version: '1.0'\nextends:\n- source: {url}\n")
    on_remote = C1.replace("extends: parent", "extends: rparent")
    _record(proc_dir, **{"child.yaml": on_remote, "demo.yaml": OLD})
    bundle = {"child.yaml": on_remote.replace("id: work", "id: build"), "demo.yaml": NEW}

    plan = upgrade.plan_update(proc_dir, bundle, [])
    assert "not fetched yet" in plan.unchecked and plan.unsafe == ""
    assert not (tmp_path / ".process-state").exists()

    source = _dev_source(tmp_path, **bundle)
    result = _run(tmp_path, "update", "--apply", "--dev", "--turnstile-dir", str(source))
    assert result.exit_code == 0, result.output
    assert "Fetching extends source" in result.output
    # The fetched parent has no 'build' state, so the child is held back.
    assert "would leave the definitions unloadable" in result.output
    assert (proc_dir / "child.yaml").read_text() == on_remote
    assert (proc_dir / "demo.yaml").read_text() == NEW
    assert _loads(proc_dir)


def test_apply_refuses_when_a_git_source_cannot_be_fetched(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nextends:\n- source: git+file:///nowhere/remote.git#v1\n"
    )
    _record(proc_dir, **{"demo.yaml": OLD})
    source = _dev_source(tmp_path, **{"demo.yaml": NEW})
    result = _run(tmp_path, "update", "--apply", "--dev", "--turnstile-dir", str(source))
    assert result.exit_code == 1
    assert "Failed to fetch" in result.output
    assert (proc_dir / "demo.yaml").read_text() == OLD


def test_cached_git_source_is_read_for_new_starters(proc_dir):
    url = "file:///nowhere/remote.git"
    (proc_dir / "registry.yaml").write_text(f"version: '1.0'\nextends:\n- source: git+{url}#v1\n")
    cached = (
        proc_dir.parent / ".process-state" / ".cache" / "git"
        / url.replace("://", "_").replace("/", "_").replace(".", "_") / "processes"
    )
    cached.mkdir(parents=True)
    (cached / "other.yaml").write_text(_definition("other", "1.0.0"))
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert (_status(plan), plan.unchecked, plan.unsafe) == (upgrade.NEW, "", "")


@pytest.mark.parametrize("forced", [frozenset(), frozenset({"demo"})])
def test_a_package_this_cli_cannot_import_refuses_every_write(tmp_path, forced):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nextends:\n- source: pypi:no-such-processes-pkg\n"
    )
    _record(proc_dir, **{"demo.yaml": OLD})
    plan = upgrade.plan_update(proc_dir, BUNDLE, [], forced)
    assert "not importable by this CLI" in plan.unsafe
    assert "uvx --with" in plan.unsafe
    source = _dev_source(tmp_path, **{"demo.yaml": NEW})
    args = ["--force", "demo"] if forced else []
    result = _run(tmp_path, "update", "--apply", *args, "--dev", "--turnstile-dir", str(source))
    assert result.exit_code == 1
    assert (proc_dir / "demo.yaml").read_text() == OLD


def test_nothing_to_write_is_not_refused(proc_dir):
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nextends:\n- source: pypi:no-such-processes-pkg\n"
    )
    _install(proc_dir, NEW)
    assert upgrade.plan_update(proc_dir, BUNDLE, []).unsafe == ""


def test_missing_local_extends_source_refuses_as_the_loader_fails(proc_dir):
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nextends:\n- source: ./gone\n")
    _record(proc_dir, **{"demo.yaml": OLD})
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert "./gone (not found)" in plan.unsafe


@pytest.mark.parametrize("where", ["junk.yaml", "overrides/junk.yaml"])
def test_a_file_the_loader_cannot_read_refuses_the_plan(proc_dir, where):
    (proc_dir / "overrides").mkdir()
    (proc_dir / where).write_bytes(b"name: \xff\xfe\n")
    _record(proc_dir, **{"demo.yaml": OLD})
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert f"cannot read {where}" in plan.unsafe


def test_an_unlisted_file_the_loader_never_reads_does_not_refuse(proc_dir):
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nlocal:\n- demo\n")
    (proc_dir / "junk.yaml").write_bytes(b"name: \xff\xfe\n")
    _record(proc_dir, **{"demo.yaml": OLD})
    assert upgrade.plan_update(proc_dir, BUNDLE, []).unsafe == ""


def test_identical_extends_strings_collapse_as_in_the_loader(tmp_path, proc_dir):
    # The loader keys sources by string: the last entry's filter wins.
    shared = tmp_path / "shared"
    shared.mkdir()
    for n in ("a", "b"):
        (shared / f"{n}.yaml").write_text(_definition(n, "1.0.0"))
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nextends:\n- source: ./shared\n  processes: [a]\n"
        "- source: ./shared\n  processes: [b]\n"
    )
    plan = upgrade.plan_update(proc_dir, {"a.yaml": _definition("a", "2.0.0")}, [])
    assert _status(plan, "a") == upgrade.NEW


def test_source_with_a_nested_processes_directory_is_read_as_the_loader_reads_it(
    tmp_path, proc_dir
):
    # The loader resolves ./shared to shared/processes and reads that
    # directory only; shared/processes/processes is never read.
    (tmp_path / "shared" / "processes" / "processes").mkdir(parents=True)
    (tmp_path / "shared" / "processes" / "parent.yaml").write_text(P1)
    (tmp_path / "shared" / "processes" / "processes" / "parent.yaml").write_text(P2)
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nextends:\n- source: ./shared\n")
    _record(proc_dir, **{"child.yaml": C1})
    plan = upgrade.plan_update(proc_dir, {"child.yaml": C2}, [])
    assert _status(plan, "child") == upgrade.BLOCKED


# ---------------------------------------------------------------------------
# Apply is all-or-nothing; the check reads the registry and listed files as
# the loader does; output never prints URL credentials (seventh review)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write read-only files")
def test_read_only_copy_is_kept_and_nothing_breaks(proc_dir):
    _record(proc_dir, **{"parent.yaml": P1, "child.yaml": C1})
    (proc_dir / "parent.yaml").chmod(0o444)
    plan = upgrade.plan_update(proc_dir, {"parent.yaml": P2, "child.yaml": C2}, [])
    assert _status(plan, "parent") == upgrade.CONFLICT
    assert "not writable" in next(e for e in plan.entries if e.name == "parent").detail
    assert _status(plan, "child") == upgrade.BLOCKED
    upgrade.apply_plan(proc_dir, plan, "v9")
    assert _loads(proc_dir)


def test_a_failed_write_leaves_every_file_as_it_was(proc_dir, monkeypatch):
    _record(proc_dir, **{"parent.yaml": P1, "child.yaml": C1})
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nlocal:\n- parent\n- child\n")
    before = {p.name: p.read_bytes() for p in proc_dir.iterdir()}
    plan = upgrade.plan_update(
        proc_dir, {"parent.yaml": P2, "child.yaml": C2, "fresh.yaml": NEW}, []
    )
    assert len(plan.writes) == 3
    calls = []
    real_replace = os.replace

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 3:
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError, match="disk full"):
        upgrade.apply_plan(proc_dir, plan, "v9", register=True)
    assert {p.name: p.read_bytes() for p in proc_dir.iterdir()} == before


def test_apply_keeps_file_modes_and_gives_new_files_the_default(proc_dir):
    _install(proc_dir)
    (proc_dir / "demo.yaml").chmod(0o640)
    plan = upgrade.plan_update(proc_dir, {**BUNDLE, "fresh.yaml": _definition("fresh", "1.0.0")}, [])
    upgrade.apply_plan(proc_dir, plan, "v9")
    mask = os.umask(0)
    os.umask(mask)
    assert (proc_dir / "demo.yaml").stat().st_mode & 0o777 == 0o640
    assert (proc_dir / "fresh.yaml").stat().st_mode & 0o777 == 0o666 & ~mask


def test_cli_reports_a_failed_apply_without_a_traceback(tmp_path, monkeypatch):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    _record(proc_dir, **{"demo.yaml": OLD})
    source = _dev_source(tmp_path, **{"demo.yaml": NEW})

    def failing(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing)
    result = _run(tmp_path, "update", "--apply", "--dev", "--turnstile-dir", str(source))
    assert result.exit_code == 1
    assert "Nothing was written: disk full" in result.output
    assert (proc_dir / "demo.yaml").read_text() == OLD


def test_forced_write_strands_are_reported_when_the_files_do_not_load(proc_dir, tmp_path):
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nlocal:\n- alpha\n- other\n")
    (proc_dir / "other.yaml").write_text(_definition("other", "1.0.0"))
    instance = _instance_of(tmp_path, "other", "work")
    bundle = {"alpha.yaml": _definition("alpha", "1.1.0"), "other.yaml": NEW}
    plan = upgrade.plan_update(proc_dir, bundle, [instance], frozenset({"alpha", "demo"}))
    assert any("do not load" in w for w in plan.warnings)
    demo = next(e for e in plan.entries if e.name == "demo")
    assert (demo.status, demo.stranded) == (
        upgrade.APPLY, [f"{instance.instance_id} (other) at 'work'"],
    )


@pytest.mark.parametrize(
    "registry", ["extends:\n", "extends:\n- ./shared\n", "settings:\n"]
)
def test_registry_the_loader_rejects_is_not_treated_as_loading(proc_dir, registry):
    (proc_dir / "registry.yaml").write_text("version: '1.0'\n" + registry)
    _record(proc_dir, **{"demo.yaml": OLD})
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert any("invalid registry.yaml" in w for w in plan.warnings)
    assert "invalid registry.yaml" in plan.unsafe


def test_listed_file_outside_processes_is_read_where_it_is(tmp_path, proc_dir):
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "team.yaml").write_text(_definition("team", "1.0.0"))
    (proc_dir / "registry.yaml").write_text("version: '1.0'\nlocal:\n- demo\n- ../shared/team\n")
    _record(proc_dir, **{"demo.yaml": OLD})
    plan = upgrade.plan_update(proc_dir, BUNDLE, [])
    assert (plan.warnings, plan.unsafe, _status(plan)) == ([], "", upgrade.APPLY)


def test_extends_source_credentials_are_never_printed(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nextends:\n- source: git+file://user:s3cret@/nowhere/r.git#v1\n"
    )
    _record(proc_dir, **{"demo.yaml": OLD})
    source = _dev_source(tmp_path, **{"demo.yaml": NEW})
    for args in ([], ["--apply"]):
        result = _run(tmp_path, "update", *args, "--dev", "--turnstile-dir", str(source))
        assert "s3cret" not in result.output, args


def test_forced_conflict_says_what_it_replaces(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "other.yaml").write_text(_definition("other", "1.0.0"))
    source = _dev_source(tmp_path, **{"other.yaml": NEW})
    result = _run(tmp_path, "update", "--force", "demo", "--dev", "--turnstile-dir", str(source))
    assert "other.yaml exists but does not define 'demo'" in result.output


def test_preview_of_a_refused_plan_does_not_suggest_apply(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nextends:\n- source: pypi:no-such-processes-pkg\n"
    )
    _record(proc_dir, **{"demo.yaml": OLD})
    source = _dev_source(tmp_path, **{"demo.yaml": NEW})
    result = _run(tmp_path, "update", "--dev", "--turnstile-dir", str(source))
    assert result.exit_code == 0
    assert "--apply would refuse" in result.output
    assert "Run with --apply" not in result.output


# ---------------------------------------------------------------------------
# Released starter history
# ---------------------------------------------------------------------------


def test_history_recognises_the_starters_shipped_in_v0_1_6():
    """Pins the generator's normalisation against real released content."""
    shown = subprocess.run(
        [
            "git", "-C", str(REPO_ROOT), "show",
            "v0.1.6:packages/turnstile-cli/src/turnstile_cli/starters.py",
        ],
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        pytest.skip("v0.1.6 tag not available in this clone")
    namespace: dict = {}
    exec(compile(shown.stdout, "starters_v0_1_6", "exec"), namespace)
    for filename, content in namespace["STARTERS"].items():
        assert upgrade.content_hash(content) in upgrade.RELEASED_STARTER_HASHES[
            filename
        ], filename


# ---------------------------------------------------------------------------
# CLI: init records the manifest, update upgrades end to end
# ---------------------------------------------------------------------------


def _run(project: Path, *args: str):
    return CliRunner().invoke(cli, ["--project", str(project), *args])


@pytest.fixture
def initialized(tmp_path):
    result = _run(tmp_path, "init", "--enforce", "monitor")
    assert result.exit_code == 0, result.output
    return tmp_path


def test_init_records_every_starter_it_installs(initialized):
    manifest, warning = upgrade.load_manifest(initialized / ".processes")
    assert warning == ""
    assert set(manifest) == set(STARTERS)
    assert {e["release"] for e in manifest.values()} == {TURNSTILE_REF}


def test_update_on_a_fresh_init_changes_nothing(initialized):
    result = _run(initialized, "update", "--apply")
    assert result.exit_code == 0, result.output
    assert "Everything is up to date." in result.output


def test_update_apply_upgrades_unedited_keeps_edited_and_repins(initialized):
    proc_dir = initialized / ".processes"
    stale = proc_dir / "bug-fix.yaml"
    stale.write_text(STARTERS["bug-fix.yaml"].replace('version: "', 'version: "0.', 1))
    entries, _ = upgrade.load_manifest(proc_dir)
    entries["bug-fix.yaml"]["sha256"] = upgrade.content_hash(stale.read_text())
    upgrade.write_manifest(proc_dir, entries)
    edited = proc_dir / "spike.yaml"
    edited.write_text(STARTERS["spike.yaml"] + "# ours\n")
    mcp = initialized / ".mcp.json"
    mcp.write_text(mcp.read_text().replace(f"@{TURNSTILE_REF}#", "@v0.0.1#"))
    settings = initialized / ".claude" / "settings.json"
    settings.write_text(settings.read_text().replace(f"@{TURNSTILE_REF}#", "@v0.0.1#"))
    assert upgrade.find_pins(settings.read_text()) == {"v0.0.1"}

    # An active instance, written without StateStore (which would create
    # the sibling state directories the preview must not create).
    elsewhere = StateStore(initialized.parent / "elsewhere")
    record = elsewhere.create(process_name="spike", initial_state="start")
    active = initialized / ".process-state" / "active"
    active.mkdir(parents=True, exist_ok=True)
    (written,) = (initialized.parent / "elsewhere" / "active").glob("*.json")
    (active / written.name).write_text(written.read_text())
    assert record.instance_id in written.name
    for sibling in ("completed", "abandoned"):
        assert not (initialized / ".process-state" / sibling).exists()

    def snapshot():
        return {
            p: p.read_bytes() for p in sorted(initialized.rglob("*")) if p.is_file()
        }

    before = snapshot()
    preview = _run(initialized, "update")
    assert preview.exit_code == 0, preview.output
    assert snapshot() == before  # the preview writes nothing
    assert not (initialized / ".process-state" / "completed").exists()

    result = _run(initialized, "update", "--apply")
    assert result.exit_code == 0, result.output
    assert stale.read_text() == STARTERS["bug-fix.yaml"]
    assert edited.read_text().endswith("# ours\n")
    assert upgrade.find_pins(mcp.read_text()) == {TURNSTILE_REF}
    assert upgrade.find_pins(settings.read_text()) == {TURNSTILE_REF}
    assert "Re-pinned: .mcp.json" in result.output
    assert "Re-pinned: .claude/settings.json" in result.output
    assert "/mcp reconnect" in result.output

    forced = _run(initialized, "update", "--apply", "--force", "spike")
    assert forced.exit_code == 0, forced.output
    assert edited.read_text() == STARTERS["spike.yaml"]


def test_dev_source_registers_the_file_stem_and_does_not_repin(tmp_path):
    source = tmp_path / "src"
    (source / ".processes").mkdir(parents=True)
    (source / ".processes" / "demo-file.yaml").write_text(NEW)
    project = tmp_path / "project"
    (project / ".processes").mkdir(parents=True)
    (project / ".processes" / "registry.yaml").write_text(
        "version: '1.0'\nlocal:\n- other\n"
    )
    (project / ".processes" / "other.yaml").write_text(_definition("other", "1.0.0"))
    mcp = _mcp(project, _spec("v0.0.1"))
    before = mcp.read_text()

    result = _run(
        project, "update", "--apply", "--dev", "--turnstile-dir", str(source)
    )

    assert result.exit_code == 0, result.output
    registry = yaml.safe_load((project / ".processes" / "registry.yaml").read_text())
    assert "demo-file" in registry["local"]
    assert mcp.read_text() == before
    manifest, _ = upgrade.load_manifest(project / ".processes")
    assert manifest["demo-file.yaml"]["release"] == "dev"


def test_update_reads_instances_from_a_custom_state_dir(tmp_path):
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "registry.yaml").write_text(
        "version: '1.0'\nsettings:\n  state_dir: .state\n"
    )
    _install(proc_dir)
    store = StateStore(tmp_path / ".state")
    instance = store.create(process_name="demo", initial_state="start")
    instance.current_state = "work"
    store.save(instance)
    source = tmp_path / "src"
    (source / ".processes").mkdir(parents=True)
    (source / ".processes" / "demo.yaml").write_text(
        _definition("demo", "1.1.0", ("start", "build", "done"))
    )

    result = _run(tmp_path, "update", "--dev", "--turnstile-dir", str(source))

    assert result.exit_code == 0, result.output
    assert "blocked (kept)" in result.output
    assert instance.instance_id in result.output
