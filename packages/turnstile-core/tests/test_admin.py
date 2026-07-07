"""Tests for turnstile_core.definition.analysis (graph generation, dry run)."""

import shutil
from pathlib import Path

import pytest

from turnstile_core.definition.analysis import (
    check_migration,
    diff_definitions,
    generate_mermaid,
    simulate_dry_run,
)
from turnstile_core.runtime.engine import Engine
from turnstile_core.definition.loader import load_definition

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def simple_defn():
    return load_definition(FIXTURES / "simple.yaml")


@pytest.fixture()
def feature_defn():
    return load_definition(FIXTURES / "feature-deploy.yaml")


# ---------------------------------------------------------------------------
# Graph generation
# ---------------------------------------------------------------------------


class TestGenerateMermaid:
    def test_basic_structure(self, simple_defn):
        result = generate_mermaid(simple_defn)
        src = result["mermaid_source"]
        assert "stateDiagram-v2" in src
        assert result["name"] == "simple"

    def test_initial_arrow(self, simple_defn):
        src = generate_mermaid(simple_defn)["mermaid_source"]
        assert "[*] --> start" in src

    def test_terminal_arrow(self, simple_defn):
        src = generate_mermaid(simple_defn)["mermaid_source"]
        assert "done --> [*]" in src

    def test_transitions_present(self, simple_defn):
        src = generate_mermaid(simple_defn)["mermaid_source"]
        assert "start --> working" in src
        assert "working --> review" in src
        assert "review --> done" in src

    def test_gate_markers(self, simple_defn):
        src = generate_mermaid(simple_defn)["mermaid_source"]
        # working has on_exit validation
        assert "exit gate" in src

    def test_counts(self, simple_defn):
        result = generate_mermaid(simple_defn)
        assert result["state_count"] == 4
        # start->working, working->review, working->start, review->done, review->working
        assert result["transition_count"] == 5

    def test_complex_definition(self, feature_defn):
        result = generate_mermaid(feature_defn)
        assert result["state_count"] == 9
        src = result["mermaid_source"]
        assert "entry gate" in src  # review_ready has on_enter
        assert "exit gate" in src   # run_tests has on_exit


# ---------------------------------------------------------------------------
# Dry run simulation
# ---------------------------------------------------------------------------


class TestDryRunAllStates:
    def test_lists_all_states(self, simple_defn):
        result = simulate_dry_run(simple_defn)
        assert len(result) == 4
        state_ids = [s["state"] for s in result]
        assert state_ids == ["start", "working", "review", "done"]

    def test_state_types(self, simple_defn):
        result = simulate_dry_run(simple_defn)
        assert result[0]["type"] == "initial"
        assert result[3]["type"] == "terminal"

    def test_transitions_listed(self, simple_defn):
        result = simulate_dry_run(simple_defn)
        working = result[1]
        assert set(working["transitions"]) == {"review", "start"}

    def test_validations_described(self, simple_defn):
        result = simulate_dry_run(simple_defn)
        working = result[1]
        # working has on_exit validation
        assert len(working["on_exit_validations"]) == 1
        val = working["on_exit_validations"][0]
        assert val["type"] == "validation"
        assert val["command"] == "echo done"
        assert val["expect"] == "not_empty"


class TestDryRunPath:
    def test_legal_path(self, simple_defn):
        result = simulate_dry_run(simple_defn, ["working", "review", "done"])
        assert len(result) == 3
        assert all(s["legal"] for s in result)

    def test_illegal_transition_flagged(self, simple_defn):
        result = simulate_dry_run(simple_defn, ["done"])
        assert len(result) == 1
        assert result[0]["legal"] is False

    def test_nonexistent_state_stops(self, simple_defn):
        result = simulate_dry_run(simple_defn, ["nonexistent"])
        assert len(result) == 1
        assert "error" in result[0]

    def test_path_shows_validations(self, simple_defn):
        result = simulate_dry_run(simple_defn, ["working", "review"])
        # working -> review: on_exit of working + on_enter of review
        step2 = result[1]
        assert step2["from_state"] == "working"
        assert step2["to_state"] == "review"
        assert len(step2["on_exit_validations"]) == 1  # working's exit gate
        assert len(step2["on_enter_validations"]) == 1  # review's enter gate

    def test_path_shows_available_transitions(self, simple_defn):
        result = simulate_dry_run(simple_defn, ["working"])
        assert set(result[0]["available_after"]) == {"review", "start"}


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


class TestEngineIntegration:
    @pytest.fixture()
    def engine(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
        return Engine(tmp_path)

    def test_engine_graph(self, engine):
        result = engine.graph("simple")
        assert "stateDiagram-v2" in result["mermaid_source"]
        assert result["name"] == "simple"

    def test_engine_dry_run(self, engine):
        result = engine.dry_run("simple")
        assert len(result) == 4

    def test_engine_dry_run_path(self, engine):
        result = engine.dry_run("simple", ["working", "review", "done"])
        assert len(result) == 3
        assert all(s["legal"] for s in result)

    def test_engine_diff(self, engine):
        result = engine.diff(
            str(FIXTURES / "simple.yaml"),
            str(FIXTURES / "simple-v2.yaml"),
        )
        assert result["version_a"] == "1.0.0"
        assert result["version_b"] == "2.0.0"
        assert "lint" in result["added_states"]
        assert len(result["removed_states"]) == 0


# ---------------------------------------------------------------------------
# Definition diffing
# ---------------------------------------------------------------------------


class TestDiffDefinitions:
    @pytest.fixture()
    def v1(self):
        return load_definition(FIXTURES / "simple.yaml")

    @pytest.fixture()
    def v2(self):
        return load_definition(FIXTURES / "simple-v2.yaml")

    def test_added_state(self, v1, v2):
        result = diff_definitions(v1, v2)
        assert "lint" in result["added_states"]

    def test_no_removed_states(self, v1, v2):
        result = diff_definitions(v1, v2)
        assert result["removed_states"] == []

    def test_transition_changes(self, v1, v2):
        result = diff_definitions(v1, v2)
        # working lost "review" and "start", gained "lint"
        working_tc = next(
            tc for tc in result["transition_changes"] if tc["state"] == "working"
        )
        assert "lint" in working_tc["added"]
        assert "review" in working_tc["removed"]
        assert "start" in working_tc["removed"]

    def test_modified_states(self, v1, v2):
        result = diff_definitions(v1, v2)
        modified_ids = [m["state"] for m in result["modified_states"]]
        assert "review" in modified_ids  # description changed
        assert "working" in modified_ids  # transitions changed

    def test_parameter_changes(self, v1, v2):
        result = diff_definitions(v1, v2)
        assert "reviewer" in result["parameter_changes"]["added"]
        assert result["parameter_changes"]["removed"] == []

    def test_summary(self, v1, v2):
        result = diff_definitions(v1, v2)
        assert "added" in result["summary"]
        assert "modified" in result["summary"]

    def test_identical(self, v1):
        result = diff_definitions(v1, v1)
        assert result["added_states"] == []
        assert result["removed_states"] == []
        assert result["modified_states"] == []
        assert result["summary"] == "no changes"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestCheckMigration:
    @pytest.fixture()
    def defn(self):
        return load_definition(FIXTURES / "simple.yaml")

    def test_no_change(self, defn):
        result = check_migration("working", "hash-abc", defn, "hash-abc")
        assert result["compatible"] is True
        assert result["migration_needed"] is False

    def test_compatible_migration(self, defn):
        result = check_migration("working", "hash-old", defn, "hash-new")
        assert result["compatible"] is True
        assert result["migration_needed"] is True
        assert result["state_exists"] is True
        assert "review" in result["available_transitions"]

    def test_incompatible_state_removed(self, defn):
        result = check_migration("deleted_state", "hash-old", defn, "hash-new")
        assert result["compatible"] is False
        assert result["migration_needed"] is True
        assert "suggestions" in result
        assert any(s["action"] == "skip" for s in result["suggestions"])
        assert any(s["action"] == "abandon" for s in result["suggestions"])


class TestEngineMigration:
    @pytest.fixture()
    def engine(self, tmp_path):
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        shutil.copy(FIXTURES / "simple.yaml", proc_dir / "simple.yaml")
        return Engine(tmp_path)

    def test_migrate_unchanged(self, engine):
        started = engine.start("simple", {"task_name": "test"})
        result = engine.migrate(started["instance_id"])
        assert result["compatible"] is True
        assert result["migration_needed"] is False

    def test_migrate_after_definition_change(self, engine, tmp_path):
        started = engine.start("simple", {"task_name": "test"})
        iid = started["instance_id"]

        # Modify the definition file (change its hash)
        defn_path = tmp_path / ".processes" / "simple.yaml"
        content = defn_path.read_text()
        defn_path.write_text(content + "\n# modified\n")

        # Reload so engine picks up the new hash
        engine.reload()

        result = engine.migrate(iid)
        assert result["migration_needed"] is True
        assert result["compatible"] is True
