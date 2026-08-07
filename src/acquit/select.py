"""Test selection: reachability queries and the fail-closed decision.

The default answer is always "everything runs". A test leaves the run only
when a Witness proving its closure is disjoint from the change can be built,
and witness construction re-verifies that claim independently of the
reachability query that proposed the skip. No code path emits a skipped test
without a verified witness.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Final

import rustworkx as rx

from acquit.errors import GraphError, PolicyError
from acquit.graph.model import BuiltGraph, EdgeKind, Node, NodeKind
from acquit.policy.model import Finding, RuleId, ScopeKind
from acquit.report import SelectionMode
from acquit.vcs import ChangedFile, ChangeStatus
from acquit.witness import Witness, build_witness

_REASON_NEW_TEST: Final = "new-test"
_REASON_NO_BASE: Final = "no-base-graph"
_REASON_WITNESS_REFUSED: Final = "witness-refused"

# Taint reached with no finding to blame still gets named, never dropped.
_FALLBACK_FINDING: Final = f"{RuleId.INTERNAL_ERROR}:acquit"

# Payload for the temporary sink behind multi-source reverse reachability.
_SINK: Final = Node(path="", kind=NodeKind.EXTERNAL)


@dataclass(frozen=True, slots=True)
class SelectedTest:
    """A test that must run, with every reason keeping it in the run."""

    path: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkippedTest:
    """A test provably unaffected by the change, backed by a witness."""

    path: str
    witness_id: str


@dataclass(frozen=True, slots=True)
class AlwaysRunTest:
    """A test forced to run by a policy finding, independent of the diff."""

    path: str
    finding: str


@dataclass(frozen=True, slots=True)
class Decision:
    """The complete selection outcome, deterministic for identical inputs."""

    mode: SelectionMode
    selected: tuple[SelectedTest, ...]
    skipped: tuple[SkippedTest, ...]
    always_run: tuple[AlwaysRunTest, ...]
    witnesses: tuple[Witness, ...]
    closures: Mapping[str, tuple[str, ...]]


def _reaching_paths(graph: BuiltGraph, sources: Iterable[str]) -> frozenset[str]:
    indices = sorted(graph.index_of[path] for path in set(sources) if path in graph.index_of)
    if not indices:
        return frozenset()
    work = graph.digraph.copy()
    sink = work.add_node(_SINK)
    for index in indices:
        work.add_edge(index, sink, EdgeKind.IMPORTS)
    return frozenset(work[index].path for index in rx.ancestors(work, sink))


def impacted_tests(graph: BuiltGraph, changed_paths: Collection[str]) -> frozenset[str]:
    """Every test path from which some changed node is reachable.

    A changed test counts as reaching itself. Changed paths absent from the
    graph contribute nothing here; the caller decides whether that absence
    forces a wider run.
    """
    reaching = _reaching_paths(graph, changed_paths)
    return frozenset(path for path in reaching if graph.nodes[path].kind is NodeKind.TEST)


def import_closure(graph: BuiltGraph, test_path: str) -> frozenset[str]:
    """The test file plus everything reachable from it along dependency edges."""
    index = graph.index_of.get(test_path)
    if index is None:
        raise GraphError(f"no graph node for {test_path!r}")
    reach = rx.descendants(graph.digraph, index)
    return frozenset(graph.digraph[node].path for node in reach) | {test_path}


def tainted_reachers(graph: BuiltGraph) -> frozenset[str]:
    """Every test path from which some tainted node is reachable.

    A tainted test reaches itself, so it is always included.
    """
    tainted = [path for path, node in graph.nodes.items() if node.tainted]
    reaching = _reaching_paths(graph, tainted)
    return frozenset(path for path in reaching if graph.nodes[path].kind is NodeKind.TEST)


def _run_all() -> Decision:
    return Decision(
        mode=SelectionMode.RUN_ALL,
        selected=(),
        skipped=(),
        always_run=(),
        witnesses=(),
        closures={},
    )


def _subject_of(finding: Finding) -> str:
    scope_subject = finding.scope.subject
    return scope_subject if scope_subject is not None else finding.subject


def _finding_order(finding: Finding) -> tuple[str, str, str]:
    return (str(finding.rule), _subject_of(finding), finding.reason)


def _under(path: str, directory: str) -> bool:
    prefix = directory.strip("/")
    return prefix == "" or path == prefix or path.startswith(prefix + "/")


def _capture_always_run(
    head: BuiltGraph, head_tests: frozenset[str], findings: tuple[Finding, ...]
) -> dict[str, str]:
    # First capture wins, in deterministic finding order, so attribution
    # never depends on input ordering.
    captured: dict[str, str] = {}
    ordered = sorted(findings, key=_finding_order)
    for finding in ordered:
        if finding.scope.kind is not ScopeKind.SUBTREE:
            continue
        subject = _subject_of(finding)
        for test in sorted(head_tests):
            if test not in captured and _under(test, subject):
                captured[test] = f"{finding.rule}:{subject}"
    for finding in ordered:
        if finding.scope.kind is not ScopeKind.SELF_TEST:
            continue
        subject = _subject_of(finding)
        if subject in head_tests and subject not in captured:
            captured[subject] = f"{finding.rule}:{subject}"
    taint_findings = [f for f in ordered if f.scope.kind is ScopeKind.CLOSURE_TAINT]
    for test in sorted(tainted_reachers(head)):
        if test in captured:
            continue
        closure = import_closure(head, test)
        attributed = _FALLBACK_FINDING
        for finding in taint_findings:
            subject = _subject_of(finding)
            if subject in closure:
                attributed = f"{finding.rule}:{subject}"
                break
        captured[test] = attributed
    return captured


def decide(
    head: BuiltGraph,
    base: BuiltGraph | None,
    changed: tuple[ChangedFile, ...],
    findings: tuple[Finding, ...],
) -> Decision:
    """Decide which tests run for this change; the default is everything.

    Global findings short-circuit to run-all, and a global-if-reached finding
    does the same once any head test can reach its subject; unreached, it has
    no selection effect. Impact is reverse reachability from the changed paths
    at head, plus at base for deletions, rename origins, and modifications
    when a base graph is given; without one, deletions and renames make every
    head test impacted. Findings force their captured tests to run. Whatever
    remains is skipped only after build_witness independently re-verifies that
    its import closure avoids every changed path.
    """
    for finding in findings:
        if finding.scope.kind is not ScopeKind.GLOBAL_IF_REACHED:
            continue
        if impacted_tests(head, (_subject_of(finding),)):
            return _run_all()
    if any(finding.scope.kind is ScopeKind.GLOBAL for finding in findings):
        return _run_all()

    head_tests = frozenset(path for path, node in head.nodes.items() if node.kind is NodeKind.TEST)

    head_changed: set[str] = set()
    base_changed: set[str] = set()
    added: set[str] = set()
    for change in changed:
        if change.status is ChangeStatus.DELETED:
            base_changed.add(change.path)
            continue
        head_changed.add(change.path)
        if change.status is ChangeStatus.ADDED:
            added.add(change.path)
        elif change.status is ChangeStatus.MODIFIED:
            base_changed.add(change.path)
        elif change.status is ChangeStatus.RENAMED and change.old_path is not None:
            base_changed.add(change.old_path)

    needs_base = any(
        change.status in (ChangeStatus.DELETED, ChangeStatus.RENAMED) for change in changed
    )
    missing_old = any(
        change.status is ChangeStatus.RENAMED and change.old_path is None for change in changed
    )
    unbounded = (needs_base and base is None) or missing_old

    reasons: dict[str, set[str]] = {}

    def add_reason(test: str, reason: str) -> None:
        reasons.setdefault(test, set()).add(reason)

    impacted: set[str] = set()
    if unbounded:
        # Deletions cannot be bounded without the base graph. Fail closed.
        impacted = set(head_tests)
        for test in impacted:
            add_reason(test, _REASON_NO_BASE)
    else:
        impacted = set(impacted_tests(head, head_changed))
        for path in sorted(head_changed):
            for test in impacted_tests(head, (path,)):
                add_reason(test, f"reachable-from:{path}")
        if base is not None and base_changed:
            impacted |= impacted_tests(base, base_changed) & head_tests
            for path in sorted(base_changed):
                for test in impacted_tests(base, (path,)) & head_tests:
                    add_reason(test, f"reachable-from:{path}")

    captured = _capture_always_run(head, head_tests, findings)

    for test in added & head_tests:
        add_reason(test, _REASON_NEW_TEST)

    selected_paths = set(impacted)
    for test in selected_paths & captured.keys():
        rule = captured[test].partition(":")[0]
        add_reason(test, f"rule:{rule}")
    always_paths = set(captured) - selected_paths

    full_changed = frozenset(head_changed | base_changed)

    skipped_entries: list[SkippedTest] = []
    witnesses: list[Witness] = []
    closures: dict[str, tuple[str, ...]] = {}
    next_index = 1
    for test in sorted(head_tests - selected_paths - always_paths):
        closure = import_closure(head, test)
        try:
            witness = build_witness(next_index, test, closure, full_changed)
        except PolicyError:
            # Should be unreachable: the closure and the reachability query
            # are duals. If they ever disagree, run the test and say why.
            selected_paths.add(test)
            add_reason(test, _REASON_WITNESS_REFUSED)
            continue
        skipped_entries.append(SkippedTest(path=test, witness_id=witness.id))
        witnesses.append(witness)
        closures[witness.closure_hash] = tuple(sorted(closure))
        next_index += 1

    selected_entries = tuple(
        SelectedTest(path=test, reasons=tuple(sorted(reasons.get(test, set()))))
        for test in sorted(selected_paths)
    )
    always_entries = tuple(
        AlwaysRunTest(path=test, finding=captured[test]) for test in sorted(always_paths)
    )
    mode = SelectionMode.SELECTIVE if skipped_entries else SelectionMode.RUN_ALL
    return Decision(
        mode=mode,
        selected=selected_entries,
        skipped=tuple(skipped_entries),
        always_run=always_entries,
        witnesses=tuple(witnesses),
        closures=dict(sorted(closures.items())),
    )
