"""Unit tests for reachability queries and the fail-closed decision."""

from collections.abc import Collection, Sequence

import pytest
import rustworkx as rx

from acquit.errors import GraphError, PolicyError
from acquit.graph.model import BuiltGraph, EdgeKind, Node, NodeKind
from acquit.policy.model import Finding, RuleId, Scope, ScopeKind
from acquit.report import SelectionMode
from acquit.select import (
    AlwaysRunTest,
    Decision,
    SkippedTest,
    decide,
    impacted_tests,
    import_closure,
    tainted_reachers,
)
from acquit.vcs import ChangedFile, ChangeStatus
from acquit.witness import Witness, verify_witness

Edge = tuple[str, str] | tuple[str, str, EdgeKind]


def make_module(path: str, tainted: bool = False) -> Node:
    return Node(path=path, kind=NodeKind.MODULE, tainted=tainted)


def make_test(path: str) -> Node:
    return Node(path=path, kind=NodeKind.TEST)


def make_conftest(path: str) -> Node:
    return Node(path=path, kind=NodeKind.CONFTEST)


def make_graph(nodes: Sequence[Node], edges: Sequence[Edge] = ()) -> BuiltGraph:
    digraph: rx.PyDiGraph[Node, EdgeKind] = rx.PyDiGraph()
    index_of = {node.path: digraph.add_node(node) for node in nodes}
    for edge in edges:
        kind = edge[2] if len(edge) == 3 else EdgeKind.IMPORTS
        digraph.add_edge(index_of[edge[0]], index_of[edge[1]], kind)
    return BuiltGraph(
        digraph=digraph,
        index_of=index_of,
        nodes={node.path: node for node in nodes},
        graph_hash="hand-built",
    )


def diamond() -> BuiltGraph:
    return make_graph(
        [
            make_test("tests/test_a.py"),
            make_test("tests/test_b.py"),
            make_module("src/a.py"),
            make_module("src/b.py"),
            make_module("src/core.py"),
        ],
        [
            ("tests/test_a.py", "src/a.py"),
            ("tests/test_b.py", "src/b.py"),
            ("src/a.py", "src/core.py"),
            ("src/b.py", "src/core.py"),
        ],
    )


def modified(path: str) -> ChangedFile:
    return ChangedFile(path=path, status=ChangeStatus.MODIFIED)


def added(path: str) -> ChangedFile:
    return ChangedFile(path=path, status=ChangeStatus.ADDED)


def deleted(path: str) -> ChangedFile:
    return ChangedFile(path=path, status=ChangeStatus.DELETED)


def renamed(new_path: str, old_path: str) -> ChangedFile:
    return ChangedFile(path=new_path, status=ChangeStatus.RENAMED, old_path=old_path)


def selected_paths(decision: Decision) -> set[str]:
    return {entry.path for entry in decision.selected}


def skipped_paths(decision: Decision) -> set[str]:
    return {entry.path for entry in decision.skipped}


def always_paths(decision: Decision) -> set[str]:
    return {entry.path for entry in decision.always_run}


def reasons_of(decision: Decision, path: str) -> tuple[str, ...]:
    return next(entry.reasons for entry in decision.selected if entry.path == path)


def witness_by_id(decision: Decision, witness_id: str) -> Witness:
    return next(w for w in decision.witnesses if w.id == witness_id)


def test_impacted_tests_shared_dependency_hits_both_legs() -> None:
    graph = diamond()
    assert impacted_tests(graph, ["src/core.py"]) == {"tests/test_a.py", "tests/test_b.py"}


def test_impacted_tests_single_leg() -> None:
    graph = diamond()
    assert impacted_tests(graph, ["src/a.py"]) == {"tests/test_a.py"}


def test_impacted_tests_changed_test_counts_itself() -> None:
    graph = diamond()
    assert impacted_tests(graph, ["tests/test_b.py"]) == {"tests/test_b.py"}


def test_impacted_tests_unknown_paths_contribute_nothing() -> None:
    graph = diamond()
    assert impacted_tests(graph, ["docs/readme.md"]) == frozenset()
    assert impacted_tests(graph, []) == frozenset()


def test_impacted_tests_leaves_graph_untouched() -> None:
    graph = diamond()
    nodes_before = graph.digraph.num_nodes()
    edges_before = graph.digraph.num_edges()
    impacted_tests(graph, ["src/core.py"])
    assert graph.digraph.num_nodes() == nodes_before
    assert graph.digraph.num_edges() == edges_before


def test_import_closure_includes_self_and_transitives() -> None:
    graph = diamond()
    assert import_closure(graph, "tests/test_a.py") == {
        "tests/test_a.py",
        "src/a.py",
        "src/core.py",
    }


def test_import_closure_unknown_path_raises() -> None:
    with pytest.raises(GraphError, match="nope"):
        import_closure(diamond(), "tests/nope.py")


def test_tainted_reachers_transitive_and_self() -> None:
    graph = make_graph(
        [
            make_test("tests/test_dyn.py"),
            make_test("tests/test_clean.py"),
            Node(path="tests/test_self.py", kind=NodeKind.TEST, tainted=True),
            make_module("src/mid.py"),
            make_module("src/dyn.py", tainted=True),
            make_module("src/clean.py"),
        ],
        [
            ("tests/test_dyn.py", "src/mid.py"),
            ("src/mid.py", "src/dyn.py"),
            ("tests/test_clean.py", "src/clean.py"),
        ],
    )
    assert tainted_reachers(graph) == {"tests/test_dyn.py", "tests/test_self.py"}


def test_tainted_reachers_empty_without_taint() -> None:
    assert tainted_reachers(diamond()) == frozenset()


def test_decide_shared_dependency_selects_both() -> None:
    decision = decide(diamond(), None, (modified("src/core.py"),), ())
    assert selected_paths(decision) == {"tests/test_a.py", "tests/test_b.py"}
    assert reasons_of(decision, "tests/test_a.py") == ("reachable-from:src/core.py",)
    assert decision.skipped == ()
    assert decision.mode is SelectionMode.RUN_ALL


def test_decide_one_leg_skips_the_other_with_witness() -> None:
    head = diamond()
    decision = decide(head, None, (modified("src/a.py"),), ())
    assert selected_paths(decision) == {"tests/test_a.py"}
    assert decision.skipped == (SkippedTest(path="tests/test_b.py", witness_id="w-000001"),)
    assert decision.mode is SelectionMode.SELECTIVE
    witness = witness_by_id(decision, "w-000001")
    closure = import_closure(head, "tests/test_b.py")
    assert verify_witness(witness, closure, {"src/a.py"})
    assert decision.closures == {witness.closure_hash: tuple(sorted(closure))}


def test_decide_conftest_edge_propagates_change() -> None:
    head = make_graph(
        [
            make_test("tests/test_x.py"),
            make_test("tests/test_y.py"),
            make_conftest("tests/conftest.py"),
            make_module("src/helper.py"),
            make_module("src/other.py"),
        ],
        [
            ("tests/test_x.py", "tests/conftest.py", EdgeKind.CONFTEST_SCOPE),
            ("tests/conftest.py", "src/helper.py"),
            ("tests/test_y.py", "src/other.py"),
        ],
    )
    decision = decide(head, None, (modified("src/helper.py"),), ())
    assert selected_paths(decision) == {"tests/test_x.py"}
    assert reasons_of(decision, "tests/test_x.py") == ("reachable-from:src/helper.py",)
    assert skipped_paths(decision) == {"tests/test_y.py"}


def taint_graph() -> BuiltGraph:
    return make_graph(
        [
            make_test("tests/test_dyn.py"),
            make_test("tests/test_clean.py"),
            make_module("src/dyn.py", tainted=True),
            make_module("src/clean.py"),
        ],
        [
            ("tests/test_dyn.py", "src/dyn.py"),
            ("tests/test_clean.py", "src/clean.py"),
        ],
    )


def test_decide_tainted_reacher_is_always_run_with_attribution() -> None:
    finding = Finding(
        rule=RuleId.NON_LITERAL_DYNAMIC_IMPORT,
        scope=Scope(kind=ScopeKind.CLOSURE_TAINT, subject="src/dyn.py"),
        subject="src/dyn.py",
        reason="importlib.import_module with a computed name",
    )
    decision = decide(taint_graph(), None, (), (finding,))
    assert decision.always_run == (
        AlwaysRunTest(path="tests/test_dyn.py", finding="R007:src/dyn.py"),
    )
    assert skipped_paths(decision) == {"tests/test_clean.py"}


def test_decide_taint_without_finding_attributes_internal_error() -> None:
    decision = decide(taint_graph(), None, (), ())
    assert decision.always_run == (AlwaysRunTest(path="tests/test_dyn.py", finding="R018:acquit"),)


def test_decide_taint_attribution_picks_first_finding_deterministically() -> None:
    exec_finding = Finding(
        rule=RuleId.EXEC_EVAL,
        scope=Scope(kind=ScopeKind.CLOSURE_TAINT, subject="src/dyn.py"),
        subject="src/dyn.py",
        reason="exec",
    )
    dyn_finding = Finding(
        rule=RuleId.NON_LITERAL_DYNAMIC_IMPORT,
        scope=Scope(kind=ScopeKind.CLOSURE_TAINT, subject="src/dyn.py"),
        subject="src/dyn.py",
        reason="dynamic import",
    )
    forward = decide(taint_graph(), None, (), (exec_finding, dyn_finding))
    backward = decide(taint_graph(), None, (), (dyn_finding, exec_finding))
    assert forward == backward
    # R007 sorts before R009, so it wins regardless of input order.
    assert forward.always_run[0].finding == "R007:src/dyn.py"


def test_decide_subtree_finding_captures_only_that_directory() -> None:
    head = make_graph(
        [
            make_test("tests/pkg/test_in.py"),
            make_test("tests/pkgother/test_near_miss.py"),
            make_test("tests/test_out.py"),
        ]
    )
    finding = Finding(
        rule=RuleId.CHANGED_CONFTEST,
        scope=Scope(kind=ScopeKind.SUBTREE, subject="tests/pkg"),
        subject="tests/pkg",
        reason="conftest changed",
    )
    decision = decide(head, None, (), (finding,))
    assert decision.always_run == (
        AlwaysRunTest(path="tests/pkg/test_in.py", finding="R005:tests/pkg"),
    )
    assert skipped_paths(decision) == {"tests/pkgother/test_near_miss.py", "tests/test_out.py"}


def test_decide_self_test_finding_captures_its_subject() -> None:
    head = make_graph([make_test("tests/test_a.py"), make_test("tests/test_b.py")])
    finding = Finding(
        rule=RuleId.CHANGED_TEST_FILE,
        scope=Scope(kind=ScopeKind.SELF_TEST, subject="tests/test_a.py"),
        subject="tests/test_a.py",
        reason="test file changed",
    )
    decision = decide(head, None, (), (finding,))
    assert decision.always_run == (
        AlwaysRunTest(path="tests/test_a.py", finding="R014:tests/test_a.py"),
    )
    assert skipped_paths(decision) == {"tests/test_b.py"}


def test_decide_deleted_file_with_base_selects_former_importers() -> None:
    base = make_graph(
        [
            make_test("tests/test_a.py"),
            make_test("tests/test_b.py"),
            make_module("src/gone.py"),
            make_module("src/b.py"),
        ],
        [
            ("tests/test_a.py", "src/gone.py"),
            ("tests/test_b.py", "src/b.py"),
        ],
    )
    head = make_graph(
        [
            make_test("tests/test_a.py"),
            make_test("tests/test_b.py"),
            make_module("src/b.py"),
        ],
        [("tests/test_b.py", "src/b.py")],
    )
    decision = decide(head, base, (deleted("src/gone.py"),), ())
    assert selected_paths(decision) == {"tests/test_a.py"}
    assert reasons_of(decision, "tests/test_a.py") == ("reachable-from:src/gone.py",)
    assert skipped_paths(decision) == {"tests/test_b.py"}
    # The deleted path still counts against every witness.
    assert decision.witnesses[0].changed == ("src/gone.py",)


def test_decide_base_only_test_cannot_be_selected() -> None:
    base = make_graph(
        [make_test("tests/test_removed.py"), make_module("src/gone.py")],
        [("tests/test_removed.py", "src/gone.py")],
    )
    head = make_graph([make_test("tests/test_kept.py")])
    decision = decide(head, base, (deleted("src/gone.py"),), ())
    all_paths = selected_paths(decision) | skipped_paths(decision) | always_paths(decision)
    assert "tests/test_removed.py" not in all_paths
    assert skipped_paths(decision) == {"tests/test_kept.py"}


def test_decide_deleted_file_without_base_runs_everything() -> None:
    decision = decide(diamond(), None, (deleted("src/gone.py"),), ())
    assert selected_paths(decision) == {"tests/test_a.py", "tests/test_b.py"}
    assert reasons_of(decision, "tests/test_a.py") == ("no-base-graph",)
    assert decision.skipped == ()
    assert decision.witnesses == ()
    assert decision.mode is SelectionMode.RUN_ALL


def test_decide_renamed_file_without_base_runs_everything() -> None:
    decision = decide(diamond(), None, (renamed("src/a2.py", "src/a.py"),), ())
    assert selected_paths(decision) == {"tests/test_a.py", "tests/test_b.py"}
    assert decision.skipped == ()


def test_decide_renamed_file_analyzes_old_at_base_and_new_at_head() -> None:
    base = make_graph(
        [
            make_test("tests/test_old_importer.py"),
            make_test("tests/test_other.py"),
            make_module("src/old.py"),
            make_module("src/other.py"),
        ],
        [
            ("tests/test_old_importer.py", "src/old.py"),
            ("tests/test_other.py", "src/other.py"),
        ],
    )
    head = make_graph(
        [
            make_test("tests/test_old_importer.py"),
            make_test("tests/test_new_importer.py"),
            make_test("tests/test_other.py"),
            make_module("src/new.py"),
            make_module("src/other.py"),
        ],
        [
            ("tests/test_new_importer.py", "src/new.py"),
            ("tests/test_other.py", "src/other.py"),
        ],
    )
    decision = decide(head, base, (renamed("src/new.py", "src/old.py"),), ())
    assert selected_paths(decision) == {"tests/test_new_importer.py", "tests/test_old_importer.py"}
    assert reasons_of(decision, "tests/test_new_importer.py") == ("reachable-from:src/new.py",)
    assert reasons_of(decision, "tests/test_old_importer.py") == ("reachable-from:src/old.py",)
    assert skipped_paths(decision) == {"tests/test_other.py"}
    # Witnesses count both sides of the rename as changed.
    assert decision.witnesses[0].changed == ("src/new.py", "src/old.py")


def test_decide_added_test_gets_new_test_reason() -> None:
    head = make_graph(
        [
            make_test("tests/test_new.py"),
            make_test("tests/test_old.py"),
            make_module("src/old.py"),
        ],
        [("tests/test_old.py", "src/old.py")],
    )
    decision = decide(head, None, (added("tests/test_new.py"),), ())
    assert reasons_of(decision, "tests/test_new.py") == (
        "new-test",
        "reachable-from:tests/test_new.py",
    )
    assert skipped_paths(decision) == {"tests/test_old.py"}


def test_decide_global_finding_short_circuits_to_run_all() -> None:
    finding = Finding(
        rule=RuleId.CHANGED_DEPENDENCY_MANIFEST,
        scope=Scope(kind=ScopeKind.GLOBAL),
        subject="pyproject.toml",
        reason="dependency bump",
    )
    decision = decide(diamond(), None, (modified("src/a.py"),), (finding,))
    assert decision == Decision(
        mode=SelectionMode.RUN_ALL,
        selected=(),
        skipped=(),
        always_run=(),
        witnesses=(),
        closures={},
    )


def _mutator_finding(subject: str) -> Finding:
    return Finding(
        rule=RuleId.SYS_PATH_MUTATION,
        scope=Scope(kind=ScopeKind.GLOBAL_IF_REACHED, subject=subject),
        subject=subject,
        reason="import-time sys.path mutation",
    )


def test_decide_reached_import_time_mutator_forces_run_all() -> None:
    head = make_graph(
        [
            make_test("tests/test_a.py"),
            make_test("tests/test_b.py"),
            make_module("src/mutator.py", tainted=True),
            make_module("src/b.py"),
        ],
        [
            ("tests/test_a.py", "src/mutator.py"),
            ("tests/test_b.py", "src/b.py"),
        ],
    )
    decision = decide(head, None, (modified("src/b.py"),), (_mutator_finding("src/mutator.py"),))
    assert decision == Decision(
        mode=SelectionMode.RUN_ALL,
        selected=(),
        skipped=(),
        always_run=(),
        witnesses=(),
        closures={},
    )


def test_decide_unreached_import_time_mutator_leaves_selection_selective() -> None:
    # The mutator node exists but no test has a path to it, so the finding
    # rides along in the report without shrinking the skipped set.
    head = make_graph(
        [
            make_test("tests/test_a.py"),
            make_test("tests/test_b.py"),
            make_module("src/a.py"),
            make_module("src/b.py"),
            make_module("src/orphan.py", tainted=True),
        ],
        [
            ("tests/test_a.py", "src/a.py"),
            ("tests/test_b.py", "src/b.py"),
        ],
    )
    with_finding = decide(head, None, (modified("src/a.py"),), (_mutator_finding("src/orphan.py"),))
    without = decide(head, None, (modified("src/a.py"),), ())
    assert with_finding.mode is SelectionMode.SELECTIVE
    assert selected_paths(with_finding) == {"tests/test_a.py"}
    assert skipped_paths(with_finding) == {"tests/test_b.py"}
    assert always_paths(with_finding) == set()
    assert skipped_paths(with_finding) == skipped_paths(without)


def test_decide_mutating_test_module_reaches_itself_and_forces_run_all() -> None:
    # Collection imports every test module, so a mutating test escalates alone.
    head = make_graph(
        [make_test("tests/test_mut.py"), make_test("tests/test_other.py")],
    )
    decision = decide(head, None, (), (_mutator_finding("tests/test_mut.py"),))
    assert decision.mode is SelectionMode.RUN_ALL
    assert decision.skipped == ()


def test_decide_impacted_and_always_run_appears_once_with_both_reasons() -> None:
    head = make_graph(
        [
            make_test("tests/pkg/test_both.py"),
            make_test("tests/test_free.py"),
            make_module("src/core.py"),
            make_module("src/free.py"),
        ],
        [
            ("tests/pkg/test_both.py", "src/core.py"),
            ("tests/test_free.py", "src/free.py"),
        ],
    )
    finding = Finding(
        rule=RuleId.CHANGED_CONFTEST,
        scope=Scope(kind=ScopeKind.SUBTREE, subject="tests/pkg"),
        subject="tests/pkg",
        reason="conftest changed",
    )
    decision = decide(head, None, (modified("src/core.py"),), (finding,))
    assert selected_paths(decision) == {"tests/pkg/test_both.py"}
    assert decision.always_run == ()
    assert reasons_of(decision, "tests/pkg/test_both.py") == (
        "reachable-from:src/core.py",
        "rule:R005",
    )


def test_decide_is_deterministic_and_ids_follow_sorted_order() -> None:
    head = make_graph(
        [
            make_test("tests/test_a.py"),
            make_test("tests/test_c.py"),
            make_test("tests/test_b.py"),
            make_test("tests/test_d.py"),
            make_module("src/a.py"),
        ],
        [("tests/test_a.py", "src/a.py")],
    )
    changed = (modified("src/a.py"),)
    first = decide(head, None, changed, ())
    second = decide(head, None, changed, ())
    assert first == second
    assert first.skipped == (
        SkippedTest(path="tests/test_b.py", witness_id="w-000001"),
        SkippedTest(path="tests/test_c.py", witness_id="w-000002"),
        SkippedTest(path="tests/test_d.py", witness_id="w-000003"),
    )
    assert [w.id for w in first.witnesses] == ["w-000001", "w-000002", "w-000003"]


def test_decide_identical_closures_share_one_closures_entry() -> None:
    # test_p and test_q import each other, so their closures are identical.
    head = make_graph(
        [
            make_test("tests/test_p.py"),
            make_test("tests/test_q.py"),
            make_test("tests/test_hit.py"),
            make_module("src/shared.py"),
            make_module("src/hit.py"),
        ],
        [
            ("tests/test_p.py", "tests/test_q.py"),
            ("tests/test_q.py", "tests/test_p.py"),
            ("tests/test_p.py", "src/shared.py"),
            ("tests/test_q.py", "src/shared.py"),
            ("tests/test_hit.py", "src/hit.py"),
        ],
    )
    decision = decide(head, None, (modified("src/hit.py"),), ())
    assert skipped_paths(decision) == {"tests/test_p.py", "tests/test_q.py"}
    assert decision.witnesses[0].closure_hash == decision.witnesses[1].closure_hash
    assert len(decision.closures) == 1
    (closure_paths,) = decision.closures.values()
    assert closure_paths == ("src/shared.py", "tests/test_p.py", "tests/test_q.py")


def test_decide_witness_refusal_moves_test_to_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(
        index: int, test: str, closure: Collection[str], changed: Collection[str]
    ) -> Witness:
        raise PolicyError("refused")

    monkeypatch.setattr("acquit.select.build_witness", refuse)
    decision = decide(diamond(), None, (modified("src/a.py"),), ())
    assert decision.skipped == ()
    assert decision.witnesses == ()
    assert decision.closures == {}
    assert reasons_of(decision, "tests/test_b.py") == ("witness-refused",)
    assert decision.mode is SelectionMode.RUN_ALL


def test_decide_empty_change_set_skips_everything_with_witnesses() -> None:
    head = diamond()
    decision = decide(head, None, (), ())
    assert selected_paths(decision) == set()
    assert skipped_paths(decision) == {"tests/test_a.py", "tests/test_b.py"}
    assert decision.mode is SelectionMode.SELECTIVE
    for entry in decision.skipped:
        witness = witness_by_id(decision, entry.witness_id)
        assert verify_witness(witness, import_closure(head, entry.path), ())


def test_decide_invariants_hold_on_a_mixed_scenario() -> None:
    head = make_graph(
        [
            make_test("tests/test_core.py"),
            make_test("tests/test_taint.py"),
            make_test("tests/pkg/test_sub.py"),
            make_test("tests/pkg/test_both.py"),
            make_test("tests/test_free.py"),
            make_test("tests/test_new.py"),
            make_test("tests/test_del.py"),
            make_module("src/core.py"),
            make_module("src/dyn.py", tainted=True),
            make_module("src/sub.py"),
            make_module("src/free.py"),
            make_module("src/misc.py"),
        ],
        [
            ("tests/test_core.py", "src/core.py"),
            ("tests/pkg/test_both.py", "src/core.py"),
            ("tests/test_taint.py", "src/dyn.py"),
            ("tests/pkg/test_sub.py", "src/sub.py"),
            ("tests/test_free.py", "src/free.py"),
            ("tests/test_del.py", "src/misc.py"),
        ],
    )
    base = make_graph(
        [make_test("tests/test_del.py"), make_module("src/gone.py")],
        [("tests/test_del.py", "src/gone.py")],
    )
    changed = (modified("src/core.py"), added("tests/test_new.py"), deleted("src/gone.py"))
    findings = (
        Finding(
            rule=RuleId.CHANGED_CONFTEST,
            scope=Scope(kind=ScopeKind.SUBTREE, subject="tests/pkg"),
            subject="tests/pkg",
            reason="conftest changed",
        ),
        Finding(
            rule=RuleId.NON_LITERAL_DYNAMIC_IMPORT,
            scope=Scope(kind=ScopeKind.CLOSURE_TAINT, subject="src/dyn.py"),
            subject="src/dyn.py",
            reason="dynamic import",
        ),
    )
    decision = decide(head, base, changed, findings)

    head_tests = {p for p, node in head.nodes.items() if node.kind is NodeKind.TEST}
    sel = selected_paths(decision)
    skp = skipped_paths(decision)
    alw = always_paths(decision)
    assert not skp & (sel | alw)
    assert decision.mode is SelectionMode.SELECTIVE
    assert sel | skp | alw == head_tests

    assert sel == {
        "tests/test_core.py",
        "tests/pkg/test_both.py",
        "tests/test_new.py",
        "tests/test_del.py",
    }
    assert alw == {"tests/test_taint.py", "tests/pkg/test_sub.py"}
    assert skp == {"tests/test_free.py"}
    assert reasons_of(decision, "tests/test_del.py") == ("reachable-from:src/gone.py",)
    assert reasons_of(decision, "tests/pkg/test_both.py") == (
        "reachable-from:src/core.py",
        "rule:R005",
    )
    for entry in decision.selected:
        assert entry.reasons

    full_changed = {"src/core.py", "tests/test_new.py", "src/gone.py"}
    by_id = {w.id: w for w in decision.witnesses}
    for skipped in decision.skipped:
        witness = by_id[skipped.witness_id]
        closure = import_closure(head, skipped.path)
        assert verify_witness(witness, closure, full_changed)
        assert decision.closures[witness.closure_hash] == tuple(sorted(closure))
