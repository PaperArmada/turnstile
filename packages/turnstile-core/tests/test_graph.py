"""Tests for turnstile_core.graph (GH #30: load-time static graph analysis).

Pins the edge-map contract (which statically-declared edges count), forward
reachability from the initial state, reverse reachability to terminals, and
the analyze() error/warning surface. Also an acceptance guard: every shipped
definition in .processes/ and examples/ must analyze with zero findings.
"""

import yaml
from pathlib import Path

import pytest

from turnstile_core.graph import (
    analyze,
    edge_map,
    reachable_states,
    terminal_reaching_states,
)
from turnstile_core.inheritance import resolve_inheritance
from turnstile_core.loader import load_definition, load_override
from turnstile_core.models import (
    ProcessDefinition,
    ProcessState,
    SignalSpec,
    StateType,
    SubprocessRouting,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Definition builders
# ---------------------------------------------------------------------------


def _linear_defn() -> ProcessDefinition:
    """start -> work -> done."""
    return ProcessDefinition(
        name="linear",
        states=[
            ProcessState(id="start", type=StateType.initial, transitions=["work"]),
            ProcessState(id="work", transitions=["done"]),
            ProcessState(id="done", type=StateType.terminal),
        ],
    )


def _island_defn() -> ProcessDefinition:
    """start -> done, plus an island (valid targets, never entered)."""
    return ProcessDefinition(
        name="island",
        states=[
            ProcessState(id="start", type=StateType.initial, transitions=["done"]),
            ProcessState(id="island_a", transitions=["island_b"]),
            ProcessState(id="island_b", transitions=["done"]),
            ProcessState(id="done", type=StateType.terminal),
        ],
    )


def _dangling_defn() -> ProcessDefinition:
    """start -> ghost, where 'ghost' does not exist.

    Normal construction rejects this at model validation, so the graph
    module's dangling-reference handling is exercised via model_construct
    (the documented programmatically-built-graph path).
    """
    states = [
        ProcessState(id="start", type=StateType.initial, transitions=["ghost"]),
        ProcessState(id="done", type=StateType.terminal),
    ]
    return ProcessDefinition.model_construct(name="dangling", states=states)


# ---------------------------------------------------------------------------
# edge_map
# ---------------------------------------------------------------------------


class TestEdgeMap:
    def test_plain_transitions_in_declaration_order(self):
        defn = ProcessDefinition(
            name="t",
            states=[
                ProcessState(
                    id="start",
                    type=StateType.initial,
                    transitions=["b", "a", "done"],
                ),
                ProcessState(id="a", transitions=["done"]),
                ProcessState(id="b", transitions=["done"]),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        assert edge_map(defn)["start"] == ["b", "a", "done"]

    def test_subprocess_list_on_complete_and_on_fail_are_edges(self):
        defn = ProcessDefinition(
            name="t",
            states=[
                ProcessState(id="start", type=StateType.initial, transitions=["sub"]),
                ProcessState(
                    id="sub",
                    type=StateType.subprocess,
                    process="child",
                    subprocess_routing=SubprocessRouting(
                        on_complete=["done"], on_fail=["retry"]
                    ),
                ),
                ProcessState(id="retry", transitions=["sub"]),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        assert edge_map(defn)["sub"] == ["done", "retry"]

    def test_subprocess_dict_on_complete_edges_follow_declaration_order(self):
        defn = ProcessDefinition(
            name="t",
            states=[
                ProcessState(id="start", type=StateType.initial, transitions=["sub"]),
                ProcessState(
                    id="sub",
                    type=StateType.subprocess,
                    process="child",
                    subprocess_routing=SubprocessRouting(
                        on_complete={
                            "approved": "ship",
                            "_default": "triage",
                            "rejected": "rework",
                        }
                    ),
                ),
                ProcessState(id="ship", transitions=["done"]),
                ProcessState(id="rework", transitions=["sub"]),
                ProcessState(id="triage", transitions=["sub"]),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        # Dict-form targets are deterministic: declaration order of the
        # mapping, with _default's value at its declared position.
        assert edge_map(defn)["sub"] == ["ship", "triage", "rework"]

    def test_dispatch_immediate_is_an_edge(self):
        defn = ProcessDefinition(
            name="t",
            states=[
                ProcessState(id="start", type=StateType.initial, transitions=["fire"]),
                ProcessState(
                    id="fire",
                    type=StateType.dispatch,
                    process="child",
                    immediate="done",
                ),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        assert edge_map(defn)["fire"] == ["done"]

    def test_duplicate_target_across_sources_appears_once_at_first_position(self):
        defn = ProcessDefinition(
            name="t",
            states=[
                ProcessState(id="start", type=StateType.initial, transitions=["sub"]),
                ProcessState(
                    id="sub",
                    type=StateType.subprocess,
                    process="child",
                    subprocess_routing=SubprocessRouting(
                        on_complete=["done", "retry"], on_fail=["retry", "done"]
                    ),
                ),
                ProcessState(id="retry", transitions=["sub"]),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        assert edge_map(defn)["sub"] == ["done", "retry"]

    def test_every_state_keyed_and_terminal_has_no_edges(self):
        edges = edge_map(_linear_defn())
        assert set(edges) == {"start", "work", "done"}
        assert edges["done"] == []

    def test_dangling_target_returned_as_written(self):
        assert edge_map(_dangling_defn())["start"] == ["ghost"]

    def test_duplicate_state_id_keeps_last_states_edges(self):
        """Documented tie-break for id collisions (model_construct only):
        the last declaration wins; analyze reports the duplication."""
        states = [
            ProcessState(
                id="start", type=StateType.initial, transitions=["dup", "done"]
            ),
            ProcessState(id="dup", transitions=["done"]),
            ProcessState(id="dup"),
            ProcessState(id="done", type=StateType.terminal),
        ]
        defn = ProcessDefinition.model_construct(name="dups", states=states)
        assert edge_map(defn)["dup"] == []


# ---------------------------------------------------------------------------
# reachable_states
# ---------------------------------------------------------------------------


class TestReachableStates:
    def test_linear_process_reaches_every_state(self):
        assert reachable_states(_linear_defn()) == {"start", "work", "done"}

    def test_island_states_are_not_reachable(self):
        assert reachable_states(_island_defn()) == {"start", "done"}

    def test_raises_when_no_initial_state_exists(self):
        """Documented precondition: reachability needs an entry point.
        Only constructible via model_construct; analyze() reports instead."""
        states = [
            ProcessState(id="floating", transitions=["done"]),
            ProcessState(id="done", type=StateType.terminal),
        ]
        defn = ProcessDefinition.model_construct(name="no-entry", states=states)
        with pytest.raises(ValueError):
            reachable_states(defn)

    def test_wait_state_transitions_carry_reachability(self):
        defn = ProcessDefinition(
            name="t",
            states=[
                ProcessState(
                    id="start", type=StateType.initial, transitions=["awaiting"]
                ),
                ProcessState(
                    id="awaiting",
                    type=StateType.wait,
                    signal=SignalSpec(name="approved"),
                    transitions=["done"],
                ),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        assert reachable_states(defn) == {"start", "awaiting", "done"}


# ---------------------------------------------------------------------------
# terminal_reaching_states
# ---------------------------------------------------------------------------


class TestTerminalReachingStates:
    def test_linear_process_every_state_reaches_terminal(self):
        assert terminal_reaching_states(_linear_defn()) == {
            "start",
            "work",
            "done",
        }

    def test_inescapable_loop_cannot_reach_terminal(self):
        defn = ProcessDefinition(
            name="t",
            states=[
                ProcessState(
                    id="start",
                    type=StateType.initial,
                    transitions=["loop_a", "done"],
                ),
                ProcessState(id="loop_a", transitions=["loop_b"]),
                ProcessState(id="loop_b", transitions=["loop_a"]),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        assert terminal_reaching_states(defn) == {"start", "done"}


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


class TestAnalyze:
    def test_clean_definition_has_no_findings(self):
        analysis = analyze(_linear_defn())
        assert analysis.errors == []
        assert analysis.warnings == []
        # Raw walk results are exposed for downstream queries.
        assert analysis.edges["start"] == ["work"]
        assert analysis.reachable == {"start", "work", "done"}
        assert analysis.terminal_reaching == {"start", "work", "done"}

    def test_unreachable_state_is_warned(self):
        analysis = analyze(_island_defn())
        assert (
            "State 'island_a' is unreachable from the initial state"
            in analysis.warnings
        )
        assert (
            "State 'island_b' is unreachable from the initial state"
            in analysis.warnings
        )
        assert analysis.errors == []

    def test_reachable_dead_end_sink_is_warned(self):
        defn = ProcessDefinition(
            name="t",
            states=[
                ProcessState(
                    id="start",
                    type=StateType.initial,
                    transitions=["sink", "done"],
                ),
                ProcessState(id="sink"),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        analysis = analyze(defn)
        assert analysis.warnings == [
            "State 'sink' cannot reach any terminal state (dead end)"
        ]
        assert analysis.errors == []

    def test_unreachable_dead_end_gets_only_the_unreachable_warning(self):
        defn = ProcessDefinition(
            name="t",
            states=[
                ProcessState(id="start", type=StateType.initial, transitions=["done"]),
                ProcessState(id="orphan"),
                ProcessState(id="done", type=StateType.terminal),
            ],
        )
        analysis = analyze(defn)
        orphan_warnings = [w for w in analysis.warnings if "orphan" in w]
        assert orphan_warnings == [
            "State 'orphan' is unreachable from the initial state"
        ]

    def test_dangling_reference_is_an_error(self):
        analysis = analyze(_dangling_defn())
        assert (
            "State 'start' references unknown state 'ghost'" in analysis.errors
        )

    def test_no_initial_state_reported_not_raised(self):
        """A programmatic graph without an entry point gets a GraphAnalysis
        back: the missing initial is an error, terminal-reachability is
        still computed, and no reachability warnings are emitted (they
        would all be noise without an entry point)."""
        states = [
            ProcessState(id="floating", transitions=["done"]),
            ProcessState(id="done", type=StateType.terminal),
        ]
        defn = ProcessDefinition.model_construct(name="no-entry", states=states)
        analysis = analyze(defn)
        assert (
            "Process must have exactly one initial state, found 0"
            in analysis.errors
        )
        assert analysis.reachable == set()
        assert analysis.terminal_reaching == {"floating", "done"}
        assert analysis.warnings == []

    def test_multiple_initial_states_reported_not_raised(self):
        states = [
            ProcessState(id="entry_a", type=StateType.initial, transitions=["done"]),
            ProcessState(id="entry_b", type=StateType.initial, transitions=["done"]),
            ProcessState(id="done", type=StateType.terminal),
        ]
        defn = ProcessDefinition.model_construct(name="two-entries", states=states)
        analysis = analyze(defn)
        assert (
            "Process must have exactly one initial state, found 2"
            in analysis.errors
        )
        assert analysis.reachable == set()
        assert analysis.terminal_reaching == {"entry_a", "entry_b", "done"}
        assert analysis.warnings == []

    def test_dangling_reference_still_reported_on_degenerate_graph(self):
        """The missing-initial early return must not swallow dangling-ref
        errors already found."""
        states = [
            ProcessState(id="floating", transitions=["ghost"]),
            ProcessState(id="done", type=StateType.terminal),
        ]
        defn = ProcessDefinition.model_construct(name="no-entry", states=states)
        analysis = analyze(defn)
        assert (
            "State 'floating' references unknown state 'ghost'"
            in analysis.errors
        )
        assert (
            "Process must have exactly one initial state, found 0"
            in analysis.errors
        )

    def test_duplicate_state_id_is_an_error(self):
        states = [
            ProcessState(
                id="start", type=StateType.initial, transitions=["dup", "done"]
            ),
            ProcessState(id="dup", transitions=["done"]),
            ProcessState(id="dup", transitions=["done"]),
            ProcessState(id="done", type=StateType.terminal),
        ]
        defn = ProcessDefinition.model_construct(name="dups", states=states)
        analysis = analyze(defn)
        assert "Duplicate state id 'dup'" in analysis.errors


# ---------------------------------------------------------------------------
# Acceptance guard: shipped definitions stay clean
# ---------------------------------------------------------------------------


def _shipped_definition_paths() -> list[Path]:
    processes = sorted((REPO_ROOT / ".processes").glob("*.yaml"))
    examples = sorted((REPO_ROOT / "examples").glob("*.yaml"))
    return [p for p in processes if p.name != "registry.yaml"] + examples


def _load_shipped(path: Path) -> ProcessDefinition:
    """Load a shipped file, resolving overrides against a same-dir parent.

    Mirrors the documented override semantic: a file with both 'extends'
    and 'overrides' keys is an override, everything else a plain
    definition.
    """
    raw = yaml.safe_load(path.read_text())
    if "extends" in raw and "overrides" in raw:
        override = load_override(path)
        parent_name = override.extends.rsplit("/", 1)[-1]
        parent = load_definition(path.parent / f"{parent_name}.yaml")
        return resolve_inheritance(override, parent)
    return load_definition(path)


class TestShippedDefinitionsAnalyzeClean:
    @pytest.mark.parametrize(
        "path",
        _shipped_definition_paths(),
        ids=lambda p: f"{p.parent.name}/{p.name}",
    )
    def test_shipped_definition_has_no_findings(self, path: Path):
        analysis = analyze(_load_shipped(path))
        assert analysis.errors == [], f"{path}: {analysis.errors}"
        assert analysis.warnings == [], f"{path}: {analysis.warnings}"

    def test_guard_actually_covers_the_shipped_set(self):
        """If the starter pack moves or the glob rots, fail loudly instead
        of silently parametrizing over nothing."""
        paths = _shipped_definition_paths()
        assert len(paths) >= 15, [p.name for p in paths]
