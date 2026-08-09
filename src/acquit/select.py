"""Test selection: reachability queries and the fail-closed decision.

The default answer is always "everything runs". A test leaves the run only
when a Witness proving its claim can be built: disjointness from the change,
or (with narrowing enabled, ADR 0008) an intersection made only of proven
import-time-only files. Witness construction re-verifies the claim
independently of the reachability query that proposed the skip. No code path
emits a skipped test without a verified witness.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Final

import rustworkx as rx

from acquit.errors import GraphError, PolicyError
from acquit.graph.model import BuiltGraph, EdgeKind, Node, NodeKind
from acquit.graph.parse import ModuleFacts
from acquit.policy.model import Finding, RuleId, ScopeKind
from acquit.report import SelectionMode
from acquit.vcs import ChangedFile, ChangeStatus
from acquit.witness import NarrowedFile, ReliedInit, Witness, build_witness

_REASON_NEW_TEST: Final = "new-test"
_REASON_NO_BASE: Final = "no-base-graph"
_REASON_WITNESS_REFUSED: Final = "witness-refused"
_NARROWING_PREFIX: Final = "narrowing-refused:"

# Narrowing refusal names. The numbered ones map to the ADR 0008 conditions;
# the rest are the overrides that refuse before any condition is consulted.
REFUSED_NOT_MODIFIED: Final = "not-modified-in-place"  # condition 1
REFUSED_NOT_INERT: Final = "not-import-inert"  # condition 2
REFUSED_BOUND_NAMES: Final = "bound-names-differ"  # condition 3
REFUSED_EDGE_SET: Final = "edge-set-differs"  # condition 4
REFUSED_IMPURE_INIT: Final = "impure-init"  # condition 5
REFUSED_SEMANTIC_CLOSURE: Final = "inside-semantic-closure"  # condition 6
REFUSED_CHANGED_INIT: Final = "changed-init"
REFUSED_TAINTED: Final = "tainted-closure"
REFUSED_TEST_MISSING_AT_BASE: Final = "test-missing-at-base"

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
    # True when the witness carries the ADR 0008 narrowed claim.
    narrowed: bool = False


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


@dataclass(frozen=True, slots=True)
class NarrowingContext:
    """Base-and-head evidence for the ADR 0008 narrowed impact rule.

    Only ref-to-ref runs with narrowing enabled construct one; decide never
    narrows without it, so working-tree selections keep today's behavior.
    """

    head_facts: Mapping[str, ModuleFacts]
    base_facts: Mapping[str, ModuleFacts]
    head_blobs: Mapping[str, str]
    base_blobs: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class NarrowingRefusal:
    """Why one candidate test kept its impact; reason feeds selection reasons."""

    reason: str
    subject: str


def _semantic_digraph(digraph: rx.PyDiGraph[Node, EdgeKind]) -> rx.PyDiGraph[Node, EdgeKind]:
    """The graph with INIT_REEXPORT edges removed; node indices are preserved."""
    filtered = digraph.copy()
    doomed = [
        index
        for index in filtered.edge_indices()
        if filtered.get_edge_data_by_index(index) is EdgeKind.INIT_REEXPORT
    ]
    for index in doomed:
        filtered.remove_edge_from_index(index)
    return filtered


def semantic_closure(graph: BuiltGraph, test_path: str) -> frozenset[str]:
    """The import closure computed without INIT_REEXPORT edges (ADR 0008).

    A file in the closure but outside this set is import-time-only for the
    test: every dependency path to it crosses a pure init's re-export edge.
    """
    index = graph.index_of.get(test_path)
    if index is None:
        raise GraphError(f"no graph node for {test_path!r}")
    filtered = _semantic_digraph(graph.digraph)
    return frozenset(filtered[node].path for node in rx.descendants(filtered, index)) | {test_path}


def _out_edges(graph: BuiltGraph, path: str) -> frozenset[tuple[str, EdgeKind]]:
    """The resolved outgoing edge set of one node: (destination, kind) pairs."""
    index = graph.index_of.get(path)
    if index is None:
        return frozenset()
    return frozenset(
        (graph.digraph[dst].path, kind) for _src, dst, kind in graph.digraph.out_edges(index)
    )


class NarrowingJudge:
    """Applies the six ADR 0008 conditions; replay reuses this verbatim.

    judge() either returns the narrowed evidence block for one candidate
    test (one entry per intersecting file, in sorted path order) or the
    refusal that keeps the test selected. Every uncertain path refuses, and
    a refusal reproduces today's behavior exactly.
    """

    def __init__(
        self,
        head: BuiltGraph,
        base: BuiltGraph,
        ctx: NarrowingContext,
        changed: tuple[ChangedFile, ...],
    ) -> None:
        self._head = head
        self._base = base
        self._ctx = ctx
        self._statuses: dict[str, ChangeStatus] = {}
        for change in changed:
            self._statuses[change.path] = change.status
            if change.old_path is not None:
                # The origin of a rename is never "modified in place".
                self._statuses.setdefault(change.old_path, ChangeStatus.RENAMED)
        self._changed_paths = frozenset(self._statuses)
        self._head_semantic = _semantic_digraph(head.digraph)
        self._base_semantic = _semantic_digraph(base.digraph)
        self._init_reach: dict[str, frozenset[str]] = {}

    def judge(self, test: str) -> tuple[NarrowedFile, ...] | NarrowingRefusal:
        """The narrowed block excusing every intersecting file, or the refusal."""
        if test not in self._base.index_of:
            # Condition 6 needs both semantic closures; no base test, no pair.
            return NarrowingRefusal(reason=REFUSED_TEST_MISSING_AT_BASE, subject=test)
        head_closure = import_closure(self._head, test)
        base_closure = import_closure(self._base, test)
        if self._reaches_taint(head_closure, self._head) or self._reaches_taint(
            base_closure, self._base
        ):
            return NarrowingRefusal(reason=REFUSED_TAINTED, subject=test)
        head_semantic = self._semantic(self._head, self._head_semantic, test)
        base_semantic = self._semantic(self._base, self._base_semantic, test)
        block: list[NarrowedFile] = []
        for path in sorted((head_closure | base_closure) & self._changed_paths):
            verdict = self._file(test, path, head_closure, head_semantic, base_semantic)
            if isinstance(verdict, NarrowingRefusal):
                return verdict
            block.append(verdict)
        return tuple(block)

    def _file(
        self,
        test: str,
        path: str,
        head_closure: frozenset[str],
        head_semantic: frozenset[str],
        base_semantic: frozenset[str],
    ) -> NarrowedFile | NarrowingRefusal:
        if path.rpartition("/")[2] == "__init__.py":
            # A changed init is never narrowed: consumers hold full edges to it.
            return NarrowingRefusal(reason=REFUSED_CHANGED_INIT, subject=path)
        if self._statuses.get(path) is not ChangeStatus.MODIFIED:
            return NarrowingRefusal(reason=REFUSED_NOT_MODIFIED, subject=path)
        head_facts = self._ctx.head_facts.get(path)
        base_facts = self._ctx.base_facts.get(path)
        head_blob = self._ctx.head_blobs.get(path)
        base_blob = self._ctx.base_blobs.get(path)
        if head_facts is None or base_facts is None or head_blob is None or base_blob is None:
            # Unparseable or unreadable at either revision: nothing provable.
            return NarrowingRefusal(reason=REFUSED_NOT_INERT, subject=path)
        if head_facts.inert_reason is not None or base_facts.inert_reason is not None:
            return NarrowingRefusal(reason=REFUSED_NOT_INERT, subject=path)
        if head_facts.bound_names != base_facts.bound_names:
            return NarrowingRefusal(reason=REFUSED_BOUND_NAMES, subject=path)
        if _out_edges(self._head, path) != _out_edges(self._base, path):
            return NarrowingRefusal(reason=REFUSED_EDGE_SET, subject=path)
        inits = self._relied_inits(path, head_closure)
        if isinstance(inits, NarrowingRefusal):
            return inits
        if path in head_semantic or path in base_semantic:
            return NarrowingRefusal(reason=REFUSED_SEMANTIC_CLOSURE, subject=path)
        return NarrowedFile(path=path, base_blob=base_blob, head_blob=head_blob, inits=inits)

    def _relied_inits(
        self, path: str, head_closure: frozenset[str]
    ) -> tuple[ReliedInit, ...] | NarrowingRefusal:
        """Condition 5: every init a head test-to-file route may cross.

        The candidate set over-approximates (every proven init between the
        test and the file), which only ever adds obligations. Empty refuses:
        the head graph offers no proven route the claim could rely on.
        """
        relied: list[ReliedInit] = []
        for init_path in sorted(self._head.reexport_inits):
            if init_path not in head_closure or path not in self._reach_of(init_path):
                continue
            head_tier = self._head.reexport_inits[init_path]
            base_tier = self._base.reexport_inits.get(init_path)
            if base_tier is None or base_tier is not head_tier:
                return NarrowingRefusal(reason=REFUSED_IMPURE_INIT, subject=init_path)
            relied.append(
                ReliedInit(path=init_path, base_tier=str(base_tier), head_tier=str(head_tier))
            )
        if not relied:
            return NarrowingRefusal(reason=REFUSED_IMPURE_INIT, subject=path)
        return tuple(relied)

    def _reach_of(self, init_path: str) -> frozenset[str]:
        cached = self._init_reach.get(init_path)
        if cached is None:
            index = self._head.index_of[init_path]
            reach = rx.descendants(self._head.digraph, index)
            cached = frozenset(self._head.digraph[node].path for node in reach)
            self._init_reach[init_path] = cached
        return cached

    @staticmethod
    def _reaches_taint(closure: frozenset[str], graph: BuiltGraph) -> bool:
        return any(graph.nodes[path].tainted for path in closure)

    @staticmethod
    def _semantic(
        graph: BuiltGraph, filtered: rx.PyDiGraph[Node, EdgeKind], test: str
    ) -> frozenset[str]:
        index = graph.index_of[test]
        reach = rx.descendants(filtered, index)
        return frozenset(filtered[node].path for node in reach) | {test}


def escalated_findings(head: BuiltGraph, findings: tuple[Finding, ...]) -> tuple[Finding, ...]:
    """The GLOBAL_IF_REACHED findings whose subject some head test reaches.

    Each one acts exactly like a GLOBAL finding for this head graph; the
    pipeline and explain reuse this check so all three agree on escalation.
    """
    return tuple(
        finding
        for finding in findings
        if finding.scope.kind is ScopeKind.GLOBAL_IF_REACHED
        and impacted_tests(head, (_subject_of(finding),))
    )


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
    narrowing: NarrowingContext | None = None,
) -> Decision:
    """Decide which tests run for this change; the default is everything.

    Global findings short-circuit to run-all, and a global-if-reached finding
    does the same once any head test can reach its subject; unreached, it has
    no selection effect. Impact is reverse reachability from the changed paths
    at head, plus at base for deletions, rename origins, and modifications
    when a base graph is given; without one, deletions and renames make every
    head test impacted. With a NarrowingContext and a base graph, an impacted
    test may still skip when every intersecting file passes all six ADR 0008
    conditions; any refusal keeps it selected with the reason appended.
    Findings force their captured tests to run. Whatever remains is skipped
    only after build_witness independently re-verifies its claim.
    """
    if escalated_findings(head, findings):
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

    narrowed_blocks: dict[str, tuple[NarrowedFile, ...]] = {}
    if narrowing is not None and base is not None and not unbounded and impacted:
        judge = NarrowingJudge(head, base, narrowing, changed)
        for test in sorted(impacted):
            outcome = judge.judge(test)
            if isinstance(outcome, NarrowingRefusal):
                add_reason(test, _NARROWING_PREFIX + outcome.reason)
            elif outcome:
                narrowed_blocks[test] = outcome
        for test in narrowed_blocks:
            # Every impact reason is excused; the witness carries the proof.
            impacted.discard(test)
            reasons.pop(test, None)

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
            witness = build_witness(
                next_index, test, closure, full_changed, narrowed_blocks.get(test, ())
            )
        except PolicyError:
            # Should be unreachable: the closure and the reachability query
            # are duals. If they ever disagree, run the test and say why.
            selected_paths.add(test)
            add_reason(test, _REASON_WITNESS_REFUSED)
            continue
        skipped_entries.append(
            SkippedTest(path=test, witness_id=witness.id, narrowed=bool(witness.narrowed))
        )
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
