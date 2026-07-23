"""Static graph analysis for process definitions.

Answers structural questions about a definition before any instance runs:
which states are reachable from the initial state, which states can still
reach a terminal (no dead-end sinks or inescapable loops), and whether
every statically-declared edge points at a real state.

Deliberately a standalone module rather than part of the Pydantic model
validator: the same graph walk backs `turnstile validate` warnings today
and is the reusable substrate for arbitrary property queries later
(roadmap #26). The functions take a loaded ProcessDefinition and have no
side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from turnstile_core.models import ProcessDefinition, StateType


def edge_map(defn: ProcessDefinition) -> dict[str, list[str]]:
    """Return every statically-declared edge out of each state.

    Includes ordinary transitions, subprocess routing targets (all
    on_complete forms plus on_fail), and the dispatch ``immediate``
    target. Order-preserving and deduplicated per state. Targets are
    returned as written, whether or not they exist in the definition;
    ``analyze`` reports dangling ones.
    """
    edges: dict[str, list[str]] = {}
    for state in defn.states:
        targets: list[str] = list(state.transitions)
        if state.subprocess_routing:
            targets.extend(state.subprocess_routing.on_complete_targets())
            targets.extend(state.subprocess_routing.on_fail)
        if state.immediate:
            targets.append(state.immediate)
        seen: set[str] = set()
        deduped: list[str] = []
        for target in targets:
            if target not in seen:
                seen.add(target)
                deduped.append(target)
        edges[state.id] = deduped
    return edges


def reachable_states(defn: ProcessDefinition) -> set[str]:
    """States reachable from the initial state (BFS over edge_map)."""
    edges = edge_map(defn)
    state_ids = {s.id for s in defn.states}
    start = defn.initial_state().id
    reached: set[str] = {start}
    frontier: list[str] = [start]
    while frontier:
        current = frontier.pop()
        for target in edges.get(current, []):
            if target in state_ids and target not in reached:
                reached.add(target)
                frontier.append(target)
    return reached


def terminal_reaching_states(defn: ProcessDefinition) -> set[str]:
    """States with a path to some terminal (reverse BFS from terminals)."""
    edges = edge_map(defn)
    state_ids = {s.id for s in defn.states}
    reverse: dict[str, set[str]] = {sid: set() for sid in state_ids}
    for source, targets in edges.items():
        for target in targets:
            if target in state_ids:
                reverse[target].add(source)
    reaching: set[str] = {
        s.id for s in defn.states if s.type == StateType.terminal
    }
    frontier = list(reaching)
    while frontier:
        current = frontier.pop()
        for source in reverse[current]:
            if source not in reaching:
                reaching.add(source)
                frontier.append(source)
    return reaching


@dataclass
class GraphAnalysis:
    """Result of a static structural analysis of one definition.

    ``errors`` are broken references (an edge to a state that does not
    exist); ``warnings`` are structural smells that load fine but fail or
    mislead at runtime (unreachable states, dead-end sinks). The raw
    ``edges``/``reachable``/``terminal_reaching`` sets are exposed so
    downstream tooling can run its own queries without re-walking.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    edges: dict[str, list[str]] = field(default_factory=dict)
    reachable: set[str] = field(default_factory=set)
    terminal_reaching: set[str] = field(default_factory=set)


def analyze(defn: ProcessDefinition) -> GraphAnalysis:
    """Run the full static pass over a definition.

    Definitions built through normal model validation cannot carry broken
    transition/routing/immediate references (those raise at load), so on
    the ordinary path ``errors`` stays empty; the checks remain here so
    the analysis is trustworthy on programmatically-built graphs too.
    """
    analysis = GraphAnalysis(edges=edge_map(defn))
    state_ids = {s.id for s in defn.states}

    for source, targets in analysis.edges.items():
        for target in targets:
            if target not in state_ids:
                analysis.errors.append(
                    f"State '{source}' references unknown state '{target}'"
                )

    analysis.reachable = reachable_states(defn)
    analysis.terminal_reaching = terminal_reaching_states(defn)

    for state in defn.states:
        if state.id not in analysis.reachable:
            analysis.warnings.append(
                f"State '{state.id}' is unreachable from the initial state"
            )
        elif state.id not in analysis.terminal_reaching:
            analysis.warnings.append(
                f"State '{state.id}' cannot reach any terminal state "
                f"(dead end)"
            )
    return analysis
