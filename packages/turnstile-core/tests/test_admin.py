"""Tests for turnstile_core.admin (graph generation, dry run)."""

import shutil
from pathlib import Path

import pytest

from turnstile_core.admin import generate_mermaid, simulate_dry_run
from turnstile_core.engine import Engine
from turnstile_core.loader import load_definition

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
