"""Tests for the agnostic enforcement contract (turnstile_core.enforcement).

The Claude Code adapter tests that used to live alongside these moved to
packages/turnstile-cli/tests/test_claude_adapter.py when the adapter left
core (GH #32).
"""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from turnstile_core.engine import Engine
from turnstile_core.enforcement import check_enforcement
from turnstile_core.loader import update_registry_enforcement
from turnstile_core.persistence import StateStore

FIXTURES = Path(__file__).parent / "fixtures"


def _setup_project(tmp_path, enforcement="off", fixture="simple.yaml"):
    """Set up a project with a process definition and registry."""
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir(exist_ok=True)
    shutil.copy(FIXTURES / fixture, proc_dir / fixture)

    if enforcement != "off":
        registry = {
            "version": "1.0",
            "settings": {"enforcement": enforcement},
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

    return Engine(tmp_path)


class TestCheckEnforcement:
    def test_off_mode_allows(self, tmp_path):
        _setup_project(tmp_path, enforcement="off")
        result = check_enforcement(tmp_path)
        assert result.decision == "allow"
        assert result.context == []

    def test_enforce_no_active_denies(self, tmp_path):
        _setup_project(tmp_path, enforcement="enforce")
        result = check_enforcement(tmp_path)
        assert result.decision == "deny"
        assert "No active" in result.reason

    def test_enforce_with_active_allows(self, tmp_path):
        engine = _setup_project(tmp_path, enforcement="enforce")
        engine.start("simple", {"task_name": "test"})

        result = check_enforcement(tmp_path)
        assert result.decision == "allow"
        assert "simple" in result.reason
        assert len(result.context) == 1
        assert result.context[0].process_name == "simple"

    def test_monitor_no_active_warns(self, tmp_path):
        _setup_project(tmp_path, enforcement="monitor")
        result = check_enforcement(tmp_path)
        assert result.decision == "warn"
        assert "No active" in result.reason

    def test_monitor_with_active_allows(self, tmp_path):
        engine = _setup_project(tmp_path, enforcement="monitor")
        engine.start("simple", {"task_name": "test"})

        result = check_enforcement(tmp_path)
        assert result.decision == "allow"
        assert len(result.context) == 1

    def test_no_processes_dir_defaults_off(self, tmp_path):
        result = check_enforcement(tmp_path)
        assert result.decision == "allow"

    def test_no_registry_defaults_off(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")

        result = check_enforcement(tmp_path)
        assert result.decision == "allow"

    def test_multiple_active_processes(self, tmp_path):
        engine = _setup_project(tmp_path, enforcement="enforce")
        engine.start("simple", {"task_name": "first"})
        engine.start("simple", {"task_name": "second"})

        result = check_enforcement(tmp_path)
        assert result.decision == "allow"
        assert "simple" in result.reason
        assert len(result.context) == 2

    def test_state_context_includes_description(self, tmp_path):
        engine = _setup_project(tmp_path, enforcement="monitor")
        engine.start("simple", {"task_name": "test"})

        result = check_enforcement(tmp_path)
        assert result.decision == "allow"
        ctx = result.context[0]
        assert ctx.current_state == "start"
        assert ctx.available_transitions == ["working"]

    def test_guidance_includes_state_info(self, tmp_path):
        engine = _setup_project(tmp_path, enforcement="monitor")
        engine.start("simple", {"task_name": "test"})

        result = check_enforcement(tmp_path)
        assert "simple" in result.guidance
        assert "start" in result.guidance


class TestStatePermissions:
    """Test enforcement with state-level edit permissions."""

    def test_default_permissions_allow_edit(self, tmp_path):
        """States without explicit permissions default to edit=True."""
        engine = _setup_project(tmp_path, enforcement="enforce")
        engine.start("simple", {"task_name": "test"})

        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "allow"

    def test_edit_false_blocks_in_enforce(self, tmp_path):
        """States with edit=false deny edits in enforce mode."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir(exist_ok=True)

        # Create a definition with a no-edit state
        defn = {
            "name": "guarded",
            "version": "1.0.0",
            "parameters": [{"name": "task", "required": True}],
            "states": [
                {
                    "id": "start",
                    "type": "initial",
                    "transitions": ["investigate"],
                },
                {
                    "id": "investigate",
                    "description": "Read-only investigation",
                    "permissions": {"edit": False},
                    "transitions": ["implement", "done"],
                },
                {
                    "id": "implement",
                    "description": "Write code",
                    "transitions": ["done"],
                },
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "guarded.yaml").write_text(
            yaml.dump(defn, default_flow_style=False)
        )

        registry = {
            "version": "1.0",
            "settings": {"enforcement": "enforce"},
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

        engine = Engine(tmp_path)
        started = engine.start("guarded", {"task": "test"})
        iid = started["instance_id"]

        # In start state (default permissions), edit is allowed
        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "allow"

    def test_edit_false_warns_in_monitor(self, tmp_path):
        """States with edit=false warn (not deny) in monitor mode."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir(exist_ok=True)

        defn = {
            "name": "guarded",
            "version": "1.0.0",
            "parameters": [{"name": "task", "required": True}],
            "states": [
                {
                    "id": "start",
                    "type": "initial",
                    "permissions": {"edit": False},
                    "transitions": ["work"],
                },
                {"id": "work", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "guarded.yaml").write_text(
            yaml.dump(defn, default_flow_style=False)
        )

        registry = {
            "version": "1.0",
            "settings": {"enforcement": "monitor"},
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

        engine = Engine(tmp_path)
        engine.start("guarded", {"task": "test"})

        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "warn"
        assert "does not allow edit" in result.reason

    def test_edit_false_denies_in_enforce(self, tmp_path):
        """States with edit=false deny edits in enforce mode."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir(exist_ok=True)

        defn = {
            "name": "guarded",
            "version": "1.0.0",
            "parameters": [{"name": "task", "required": True}],
            "states": [
                {
                    "id": "start",
                    "type": "initial",
                    "permissions": {"edit": False},
                    "transitions": ["work"],
                },
                {"id": "work", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "guarded.yaml").write_text(
            yaml.dump(defn, default_flow_style=False)
        )

        registry = {
            "version": "1.0",
            "settings": {"enforcement": "enforce"},
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

        engine = Engine(tmp_path)
        engine.start("guarded", {"task": "test"})

        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "deny"
        assert "does not allow edit" in result.reason

    def test_multiple_instances_most_restrictive_wins(self, tmp_path):
        """A restrictive instance is not lifted by a permissive one (F1).

        Two concurrent instances: one in an edit=false state, one that
        permits edits. Most-restrictive-wins means the edit is denied — a
        second permissive instance must not neutralize a restrictive state.
        """
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir(exist_ok=True)

        # One process with edit=false start, one with edit=true start
        defn_no_edit = {
            "name": "read-only",
            "version": "1.0.0",
            "parameters": [{"name": "task", "required": True}],
            "states": [
                {
                    "id": "start",
                    "type": "initial",
                    "permissions": {"edit": False},
                    "transitions": ["done"],
                },
                {"id": "done", "type": "terminal"},
            ],
        }
        defn_editable = {
            "name": "editable",
            "version": "1.0.0",
            "parameters": [{"name": "task", "required": True}],
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "read-only.yaml").write_text(
            yaml.dump(defn_no_edit, default_flow_style=False)
        )
        (proc_dir / "editable.yaml").write_text(
            yaml.dump(defn_editable, default_flow_style=False)
        )

        registry = {
            "version": "1.0",
            "settings": {"enforcement": "enforce"},
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

        engine = Engine(tmp_path)
        engine.start("read-only", {"task": "a"})
        engine.start("editable", {"task": "b"})

        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "deny"
        assert "read-only" in result.reason

    def test_disjoint_edit_paths_are_intersected(self, tmp_path):
        """With two instances restricting edit_paths, a file must satisfy both.

        Instance A allows only src/**, instance B only docs/**. Editing
        src/app.py is denied because B forbids it — path restrictions are
        intersected across concurrent instances, not unioned (F1).
        """
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir(exist_ok=True)

        def _defn(name, pattern):
            return {
                "name": name,
                "version": "1.0.0",
                "parameters": [{"name": "task", "required": True}],
                "states": [
                    {
                        "id": "start",
                        "type": "initial",
                        "permissions": {"edit": True, "edit_paths": [pattern]},
                        "transitions": ["done"],
                    },
                    {"id": "done", "type": "terminal"},
                ],
            }

        (proc_dir / "src-only.yaml").write_text(
            yaml.dump(_defn("src-only", "src/**"), default_flow_style=False)
        )
        (proc_dir / "docs-only.yaml").write_text(
            yaml.dump(_defn("docs-only", "docs/**"), default_flow_style=False)
        )
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(
                {"version": "1.0", "settings": {"enforcement": "enforce"}},
                default_flow_style=False,
            )
        )

        engine = Engine(tmp_path)
        engine.start("src-only", {"task": "a"})
        engine.start("docs-only", {"task": "b"})

        result = check_enforcement(
            tmp_path, action="edit", file_path=str(tmp_path / "src" / "app.py")
        )
        assert result.decision == "deny"
        assert "docs-only" in result.reason


class TestSmartSuggestions:
    """Test that enforcement guidance lists available processes."""

    def test_no_active_lists_processes(self, tmp_path):
        """When no process is active, guidance should list available processes."""
        _setup_project(tmp_path, enforcement="monitor")
        result = check_enforcement(tmp_path)
        assert result.decision == "warn"
        assert "Available processes:" in result.guidance
        assert "simple" in result.guidance

    def test_no_active_enforce_lists_processes(self, tmp_path):
        """Enforce mode denial should also list available processes."""
        _setup_project(tmp_path, enforcement="enforce")
        result = check_enforcement(tmp_path)
        assert result.decision == "deny"
        assert "Available processes:" in result.guidance
        assert "simple" in result.guidance

    def test_suggestions_include_required_params(self, tmp_path):
        """Suggestions should show required parameter names."""
        _setup_project(tmp_path, enforcement="monitor")
        result = check_enforcement(tmp_path)
        assert "requires:" in result.guidance
        assert "task_name" in result.guidance

    def test_multiple_processes_listed(self, tmp_path):
        """All available processes should be listed."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir(exist_ok=True)
        shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
        shutil.copy(FIXTURES / "quick-fix.yaml", proc_dir / "quick-fix.yaml")

        registry = {
            "version": "1.0",
            "settings": {"enforcement": "monitor"},
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

        result = check_enforcement(tmp_path)
        assert "simple" in result.guidance
        assert "quick-fix" in result.guidance

    def test_file_path_passed_through(self, tmp_path):
        """file_path parameter should be accepted without error."""
        _setup_project(tmp_path, enforcement="monitor")
        result = check_enforcement(
            tmp_path, action="edit", file_path="/some/file.py"
        )
        assert result.decision == "warn"
        assert "Available processes:" in result.guidance


class TestQuickFixProcess:
    """Test the quick-fix process definition works end-to-end."""

    @pytest.fixture()
    def engine(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        shutil.copy(FIXTURES / "quick-fix.yaml", proc_dir / "quick-fix.yaml")
        return Engine(tmp_path)

    def test_start_and_complete(self, engine):
        started = engine.start("quick-fix", {"description": "fix typo"})
        assert started["current_state"] == "start"
        assert "done" in started["available_transitions"]
        assert "abandoned" in started["available_transitions"]

    @pytest.mark.asyncio
    async def test_start_to_done(self, engine):
        started = engine.start("quick-fix", {"description": "fix typo"})
        result = await engine.transition(started["instance_id"], "done")
        assert result.success is True
        assert result.new_state == "done"
        assert result.available_transitions == []

    def test_start_to_abandoned(self, engine):
        started = engine.start("quick-fix", {"description": "not needed"})
        result = engine.abandon(started["instance_id"], "changed mind")
        assert result["success"] is True

    def test_edit_allowed_in_start(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir(exist_ok=True)
        shutil.copy(FIXTURES / "quick-fix.yaml", proc_dir / "quick-fix.yaml")

        registry = {
            "version": "1.0",
            "settings": {"enforcement": "enforce"},
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

        engine = Engine(tmp_path)
        engine.start("quick-fix", {"description": "test"})

        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "allow"
        assert "quick-fix" in result.reason


class TestUpdateRegistryEnforcement:
    def test_creates_registry_if_missing(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        update_registry_enforcement(tmp_path, "enforce")

        registry_path = proc_dir / "registry.yaml"
        assert registry_path.exists()
        raw = yaml.safe_load(registry_path.read_text())
        assert raw["settings"]["enforcement"] == "enforce"

    def test_updates_existing_registry(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        registry = {
            "version": "1.0",
            "local": ["my-process"],
            "settings": {
                "enforcement": "off",
                "log_retention_days": 30,
            },
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

        update_registry_enforcement(tmp_path, "monitor")

        raw = yaml.safe_load((proc_dir / "registry.yaml").read_text())
        assert raw["settings"]["enforcement"] == "monitor"
        # Other settings preserved
        assert raw["settings"]["log_retention_days"] == 30
        assert raw["local"] == ["my-process"]

    def test_creates_processes_dir_if_missing(self, tmp_path):
        update_registry_enforcement(tmp_path, "enforce")
        assert (tmp_path / ".processes" / "registry.yaml").exists()


class TestAssertiveBanner:
    """Test that guard guidance uses assertive framing for agent grounding."""

    def test_guidance_uses_assertive_framing(self, tmp_path):
        engine = _setup_project(tmp_path, enforcement="monitor")
        engine.start("simple", {"task_name": "test"})

        result = check_enforcement(tmp_path)
        assert "You are in:" in result.guidance
        assert "simple @ start" in result.guidance

    def test_guidance_includes_instance_id(self, tmp_path):
        engine = _setup_project(tmp_path, enforcement="enforce")
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        result = check_enforcement(tmp_path)
        assert f"instance {iid}" in result.guidance

    def test_guidance_shows_transitions(self, tmp_path):
        engine = _setup_project(tmp_path, enforcement="monitor")
        engine.start("simple", {"task_name": "test"})

        result = check_enforcement(tmp_path)
        assert "Next: working" in result.guidance


class TestEditPaths:
    """Test path-scoped edit permissions."""

    def _setup_path_restricted(self, tmp_path, enforcement="enforce", edit_paths=None):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir(exist_ok=True)

        defn = {
            "name": "scoped",
            "version": "1.0.0",
            "parameters": [{"name": "task", "required": True}],
            "states": [
                {
                    "id": "start",
                    "type": "initial",
                    "permissions": {"edit": True, "edit_paths": edit_paths or []},
                    "transitions": ["done"],
                },
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "scoped.yaml").write_text(
            yaml.dump(defn, default_flow_style=False)
        )

        registry = {
            "version": "1.0",
            "settings": {"enforcement": enforcement},
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

        engine = Engine(tmp_path)
        engine.start("scoped", {"task": "test"})
        return engine

    def test_no_edit_paths_allows_all(self, tmp_path):
        """Empty edit_paths means no restriction."""
        self._setup_path_restricted(tmp_path, edit_paths=[])
        result = check_enforcement(
            tmp_path, action="edit",
            file_path=str(tmp_path / "anything" / "file.py"),
        )
        assert result.decision == "allow"

    def test_matching_path_allows(self, tmp_path):
        """File matching an edit_paths pattern is allowed."""
        self._setup_path_restricted(tmp_path, edit_paths=["src/*"])
        result = check_enforcement(
            tmp_path, action="edit",
            file_path=str(tmp_path / "src" / "main.py"),
        )
        assert result.decision == "allow"

    def test_non_matching_path_denies(self, tmp_path):
        """File outside edit_paths is denied in enforce mode."""
        self._setup_path_restricted(tmp_path, edit_paths=["src/*"])
        result = check_enforcement(
            tmp_path, action="edit",
            file_path=str(tmp_path / "docs" / "readme.md"),
        )
        assert result.decision == "deny"
        assert "outside allowed edit paths" in result.reason

    def test_non_matching_path_warns_in_monitor(self, tmp_path):
        """File outside edit_paths warns in monitor mode."""
        self._setup_path_restricted(
            tmp_path, enforcement="monitor", edit_paths=["src/*"]
        )
        result = check_enforcement(
            tmp_path, action="edit",
            file_path=str(tmp_path / "docs" / "readme.md"),
        )
        assert result.decision == "warn"
        assert "outside allowed edit paths" in result.reason

    def test_glob_pattern_matching(self, tmp_path):
        """Glob patterns like src/** match nested paths."""
        self._setup_path_restricted(tmp_path, edit_paths=["src/**"])
        result = check_enforcement(
            tmp_path, action="edit",
            file_path=str(tmp_path / "src" / "sub" / "deep.py"),
        )
        assert result.decision == "allow"

    def test_multiple_patterns(self, tmp_path):
        """File matching any one of multiple patterns is allowed."""
        self._setup_path_restricted(
            tmp_path, edit_paths=["src/*", "tests/*"]
        )
        result = check_enforcement(
            tmp_path, action="edit",
            file_path=str(tmp_path / "tests" / "test_foo.py"),
        )
        assert result.decision == "allow"

    def test_no_file_path_skips_check(self, tmp_path):
        """When no file_path is provided, path check is skipped."""
        self._setup_path_restricted(tmp_path, edit_paths=["src/*"])
        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "allow"


class TestPathCatalogue:
    """Test path_catalogue in registry settings for process suggestions."""

    def _setup_with_catalogue(self, tmp_path, catalogue, enforcement="monitor"):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir(exist_ok=True)
        shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
        shutil.copy(FIXTURES / "quick-fix.yaml", proc_dir / "quick-fix.yaml")

        registry = {
            "version": "1.0",
            "settings": {
                "enforcement": enforcement,
                "path_catalogue": catalogue,
            },
        }
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(registry, default_flow_style=False)
        )

    def test_no_catalogue_lists_all(self, tmp_path):
        """Without catalogue, all processes are suggested."""
        self._setup_with_catalogue(tmp_path, {})
        result = check_enforcement(
            tmp_path, action="edit", file_path="src/main.py"
        )
        assert "simple" in result.guidance
        assert "quick-fix" in result.guidance

    def test_catalogue_narrows_suggestions(self, tmp_path):
        """Catalogue narrows suggestions to matching processes."""
        self._setup_with_catalogue(tmp_path, {
            "src/*": ["simple"],
            "docs/*": ["quick-fix"],
        })
        result = check_enforcement(
            tmp_path, action="edit", file_path="src/main.py"
        )
        assert "simple" in result.guidance
        assert "quick-fix" not in result.guidance
        assert "Suggested processes for" in result.guidance

    def test_catalogue_no_match_lists_all(self, tmp_path):
        """When file doesn't match any catalogue entry, list all."""
        self._setup_with_catalogue(tmp_path, {
            "src/*": ["simple"],
        })
        result = check_enforcement(
            tmp_path, action="edit", file_path="lib/util.py"
        )
        assert "Available processes:" in result.guidance
        assert "simple" in result.guidance
        assert "quick-fix" in result.guidance

    def test_catalogue_mismatch_hint(self, tmp_path):
        """Active process not in catalogue shows hint."""
        self._setup_with_catalogue(tmp_path, {
            "src/*": ["quick-fix"],
        })
        engine = Engine(tmp_path)
        engine.start("simple", {"task_name": "test"})

        result = check_enforcement(
            tmp_path, action="edit", file_path="src/main.py"
        )
        assert result.decision == "allow"
        assert "Catalogue hint" in result.guidance
        assert "quick-fix" in result.guidance

    def test_catalogue_match_no_hint(self, tmp_path):
        """Active process matching catalogue doesn't show hint."""
        self._setup_with_catalogue(tmp_path, {
            "src/*": ["simple"],
        })
        engine = Engine(tmp_path)
        engine.start("simple", {"task_name": "test"})

        result = check_enforcement(
            tmp_path, action="edit", file_path="src/main.py"
        )
        assert result.decision == "allow"
        assert "Catalogue hint" not in result.guidance

    def test_catalogue_multiple_patterns(self, tmp_path):
        """File matching multiple catalogue entries merges process lists."""
        self._setup_with_catalogue(tmp_path, {
            "src/*": ["simple"],
            "src/*.py": ["quick-fix"],
        })
        result = check_enforcement(
            tmp_path, action="edit", file_path="src/main.py"
        )
        assert "simple" in result.guidance
        assert "quick-fix" in result.guidance


class TestCorruptStateFile:
    """A torn/unreadable state file must not silently disable enforcement (F5)."""

    def _corrupt_active_file(self, tmp_path):
        """Write an unparseable file into .process-state/active/."""
        active_dir = tmp_path / ".process-state" / "active"
        active_dir.mkdir(parents=True, exist_ok=True)
        (active_dir / "torn-abc123.json").write_text('{"instance_id": "abc12')

    def test_enforce_mode_denies_on_corrupt_file(self, tmp_path):
        """In enforce mode, an unreadable state file denies (fail closed).

        A valid permissive instance is present too: the corrupt file could be
        hiding a restrictive instance, so its unreadability must override the
        permissive one rather than be silently dropped.
        """
        engine = _setup_project(tmp_path, enforcement="enforce")
        engine.start("simple", {"task_name": "ok"})
        self._corrupt_active_file(tmp_path)

        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "deny"
        assert "unknown" in result.reason.lower()

    def test_monitor_mode_warns_on_corrupt_file(self, tmp_path):
        """In monitor mode, an unreadable state file warns, does not allow."""
        engine = _setup_project(tmp_path, enforcement="monitor")
        engine.start("simple", {"task_name": "ok"})
        self._corrupt_active_file(tmp_path)

        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "warn"
        assert "unknown" in result.reason.lower()

    def test_list_active_skips_corrupt_and_keeps_valid(self, tmp_path):
        """list_active tolerates a corrupt file and still returns valid ones."""
        engine = _setup_project(tmp_path, enforcement="enforce")
        engine.start("simple", {"task_name": "ok"})
        self._corrupt_active_file(tmp_path)

        store = StateStore(tmp_path / ".process-state")
        instances, corrupt = store.list_active_with_errors()
        assert len(instances) == 1
        assert len(corrupt) == 1
        # The convenience wrapper never raises on the corrupt file.
        assert len(store.list_active()) == 1


class TestFailClosedOnUnknownConfig:
    """A torn registry or definition must not fail open either (F5, full).

    The state-file case is covered above; these pin the other two inputs the
    enforcement decision depends on. Pre-fix, each raised (registry, definition)
    or defaulted permissive (unresolved definition) and the guard's outer
    except swallowed it into an allow.
    """

    def _project(self, tmp_path, mode="enforce"):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir(exist_ok=True)
        shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(
                {"version": "1.0", "local": ["simple"],
                 "settings": {"enforcement": mode}},
                default_flow_style=False,
            )
        )
        return proc_dir

    def test_corrupt_registry_denies(self, tmp_path):
        """A torn registry.yaml denies — the mode is indeterminable."""
        proc_dir = self._project(tmp_path, "enforce")
        (proc_dir / "registry.yaml").write_text(
            "version: '1.0'\nsettings:\n  enforcement: enforce\n  bad: [x\n"
        )
        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "deny"
        assert "registry" in result.reason.lower()

    def test_corrupt_definition_denies_in_enforce(self, tmp_path):
        proc_dir = self._project(tmp_path, "enforce")
        (proc_dir / "simple.yaml").write_text("name: simple\nstates: [bad\n")
        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "deny"

    def test_corrupt_definition_warns_in_monitor(self, tmp_path):
        proc_dir = self._project(tmp_path, "monitor")
        (proc_dir / "simple.yaml").write_text("name: simple\nstates: [bad\n")
        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "warn"

    def _autodiscover_project(self, tmp_path, mode):
        """Registry with NO `local:` so discovery scans the directory.

        With auto-discovery, deleting a definition after starting an instance
        leaves discovery succeeding while the instance's definition is simply
        absent — which exercises the `unresolved` branch, not the discover-raise
        branch a registry-listed deletion would hit.
        """
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir(exist_ok=True)
        shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
        (proc_dir / "registry.yaml").write_text(
            yaml.dump(
                {"version": "1.0", "settings": {"enforcement": mode}},
                default_flow_style=False,
            )
        )
        return proc_dir

    def test_unresolved_definition_for_active_instance_denies(self, tmp_path):
        """A live instance whose definition vanished must not default permissive."""
        proc_dir = self._autodiscover_project(tmp_path, "enforce")
        Engine(tmp_path).start("simple", {"task_name": "x"})
        (proc_dir / "simple.yaml").unlink()
        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "deny"
        # Assert on the branch's own message, not a tmp-path-derived substring.
        assert "Cannot resolve definition" in result.reason
        assert "simple" in result.reason

    def test_unresolved_definition_for_active_instance_warns_in_monitor(
        self, tmp_path
    ):
        proc_dir = self._autodiscover_project(tmp_path, "monitor")
        Engine(tmp_path).start("simple", {"task_name": "x"})
        (proc_dir / "simple.yaml").unlink()
        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "warn"
        assert "Cannot resolve definition" in result.reason

    def test_state_dir_as_non_directory_denies(self, tmp_path):
        """.process-state existing as a file must not fail open (F5)."""
        self._project(tmp_path, "enforce")
        (tmp_path / ".process-state").write_text("not a directory")
        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "deny"
        assert "enforcement state is unknown" in result.reason.lower()

    def test_unexpected_evaluation_error_fails_closed(self, tmp_path, monkeypatch):
        """Backstop: any unanticipated raise during evaluation denies, not allows.

        Guards against a future unwrapped raise site reintroducing the
        fail-open chain — once enforcement is known on, evaluation failure is
        fail-closed regardless of which line raised.
        """
        import turnstile_core.enforcement as enf

        self._project(tmp_path, "enforce")
        Engine(tmp_path).start("simple", {"task_name": "x"})

        def boom(*_a, **_k):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(enf, "_build_context", boom)
        result = check_enforcement(tmp_path, action="edit")
        assert result.decision == "deny"
        assert "unknown" in result.reason.lower()
