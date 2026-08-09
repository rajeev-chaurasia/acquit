"""Explanations for individual test decisions.

Pure rendering over a completed SelectResult: no git, no filesystem. The CLI
runs the pipeline and hands the outcome here.
"""

from acquit.errors import ExitCode
from acquit.graph.model import BuiltGraph, NodeKind
from acquit.pipeline import SelectResult
from acquit.policy.model import ScopeKind
from acquit.select import escalated_findings

_REACHABLE_PREFIX = "reachable-from:"


def _dependency_path(graph: BuiltGraph, src: str, dst: str) -> tuple[str, ...] | None:
    start = graph.index_of.get(src)
    goal = graph.index_of.get(dst)
    if start is None or goal is None:
        return None
    if start == goal:
        return (src,)
    parent: dict[int, int] = {}
    seen = {start}
    frontier = [start]
    while frontier:
        upcoming: list[int] = []
        for node in frontier:
            # Neighbors sorted by path make the BFS tie-break deterministic.
            neighbors = sorted(
                graph.digraph.successor_indices(node), key=lambda index: graph.digraph[index].path
            )
            for neighbor in neighbors:
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                parent[neighbor] = node
                if neighbor == goal:
                    return _reconstruct(graph, parent, start, goal)
                upcoming.append(neighbor)
        frontier = upcoming
    return None


def _reconstruct(
    graph: BuiltGraph, parent: dict[int, int], start: int, goal: int
) -> tuple[str, ...]:
    chain = [goal]
    while chain[-1] != start:
        chain.append(parent[chain[-1]])
    return tuple(graph.digraph[index].path for index in reversed(chain))


def _explain_skipped(path: str, witness_id: str, result: SelectResult) -> tuple[str, ...]:
    witness = next(w for w in result.decision.witnesses if w.id == witness_id)
    closure = result.decision.closures[witness.closure_hash]
    changed = ", ".join(witness.changed) if witness.changed else "(empty)"
    return (
        f"{path}: skipped",
        f"  witness: {witness.id}",
        f"  claim: {witness.claim}",
        f"  closure: {len(closure)} files, hash {witness.closure_hash}",
        f"  changed: {changed}",
    )


def _explain_selected(path: str, reasons: tuple[str, ...], graph: BuiltGraph) -> tuple[str, ...]:
    lines = [f"{path}: selected"]
    for reason in reasons:
        lines.append(f"  reason: {reason}")
        if not reason.startswith(_REACHABLE_PREFIX):
            continue
        target = reason.removeprefix(_REACHABLE_PREFIX)
        chain = _dependency_path(graph, path, target)
        if chain is None:
            lines.append("    (reachable only in the base graph)")
        else:
            lines.append("    " + " imports ".join(chain))
    return tuple(lines)


def explain_lines(test: str, result: SelectResult) -> tuple[tuple[str, ...], ExitCode]:
    """Explain why one test was selected, skipped, or forced to run."""
    path = test.replace("\\", "/").removeprefix("./")
    node = result.head.graph.nodes.get(path)
    if node is None or node.kind is not NodeKind.TEST:
        return ((f"acquit: {path!r} is not a known test file at head",), ExitCode.USAGE)

    global_findings = [f for f in result.outcome.findings if f.scope.kind is ScopeKind.GLOBAL]
    # A GLOBAL_IF_REACHED finding whose subject some test reaches is global
    # in effect; render it beside the plain global findings, naming the cause.
    escalated = escalated_findings(result.head.graph, result.outcome.findings)
    if global_findings or escalated:
        lines = [f"{path}: runs; global findings force the full suite:"]
        lines.extend(f"  {f.rule} {f.subject}: {f.reason}" for f in global_findings)
        lines.extend(
            f"  {f.rule} {f.scope.subject or f.subject} (escalated: a test reaches it): {f.reason}"
            for f in escalated
        )
        return (tuple(lines), ExitCode.OK)

    for skipped in result.decision.skipped:
        if skipped.path == path:
            return (_explain_skipped(path, skipped.witness_id, result), ExitCode.OK)
    for always in result.decision.always_run:
        if always.path == path:
            return ((f"{path}: always runs", f"  finding: {always.finding}"), ExitCode.OK)
    for selected in result.decision.selected:
        if selected.path == path:
            return (_explain_selected(path, selected.reasons, result.head.graph), ExitCode.OK)
    # Unreachable when the decision covers every head test; answer safely anyway.
    return ((f"{path}: runs (not covered by the decision, run-all default)",), ExitCode.OK)
