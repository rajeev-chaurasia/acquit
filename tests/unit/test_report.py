from collections.abc import Sequence
from dataclasses import replace

import rustworkx as rx

from acquit.config import Waiver
from acquit.constants import REPORT_SCHEMA, SELECTION_SCHEMA, WITNESSES_SCHEMA
from acquit.graph.index import ModuleIndex
from acquit.graph.model import BuiltGraph, EdgeKind, Node, NodeKind
from acquit.pipeline import SelectResult, Snapshot
from acquit.policy.engine import PolicyOutcome, WaivedFinding
from acquit.policy.model import Finding, RuleId, Scope, ScopeKind
from acquit.report import (
    RunInfo,
    SelectionMode,
    build_report,
    build_run_all_report,
    build_run_all_selection,
    build_selection_doc,
    build_witnesses_doc,
    to_canonical_json,
)
from acquit.select import Decision, SkippedTest, decide
from acquit.vcs import ChangedFile, ChangeStatus
from acquit.witness import (
    CLAIM_NARROWED,
    NarrowedFile,
    ReliedInit,
    build_witness,
    closure_hash,
)

FINDING = Finding(
    rule=RuleId.INTERNAL_ERROR,
    scope=Scope(kind=ScopeKind.GLOBAL),
    subject="acquit",
    reason="test reason",
)


def _graph(nodes: Sequence[Node], edges: Sequence[tuple[str, str]]) -> BuiltGraph:
    digraph: rx.PyDiGraph[Node, EdgeKind] = rx.PyDiGraph()
    index_of = {node.path: digraph.add_node(node) for node in nodes}
    for src, dst in edges:
        digraph.add_edge(index_of[src], index_of[dst], EdgeKind.IMPORTS)
    return BuiltGraph(
        digraph=digraph,
        index_of=index_of,
        nodes={node.path: node for node in nodes},
        graph_hash="hand-built",
    )


def _result() -> SelectResult:
    graph = _graph(
        [
            Node(path="tests/test_a.py", kind=NodeKind.TEST),
            Node(path="tests/test_b.py", kind=NodeKind.TEST),
            Node(path="src/a.py", kind=NodeKind.MODULE),
            Node(path="src/b.py", kind=NodeKind.MODULE),
        ],
        [("tests/test_a.py", "src/a.py"), ("tests/test_b.py", "src/b.py")],
    )
    changed = (ChangedFile(path="src/a.py", status=ChangeStatus.MODIFIED),)
    decision = decide(graph, None, changed, ())
    waived = WaivedFinding(
        finding=Finding(
            rule=RuleId.CHANGED_RESOURCE,
            scope=Scope(kind=ScopeKind.GLOBAL),
            subject="data.csv",
            reason="data.csv is a resource file",
        ),
        waiver=Waiver(rule="R001", glob="data.*", justification="fixture data"),
    )
    snapshot = Snapshot(
        ref="feature",
        files=("src/a.py", "src/b.py", "tests/test_a.py", "tests/test_b.py"),
        kinds={
            "src/a.py": NodeKind.MODULE,
            "src/b.py": NodeKind.MODULE,
            "tests/test_a.py": NodeKind.TEST,
            "tests/test_b.py": NodeKind.TEST,
        },
        facts={},
        unparseable=(),
        index=ModuleIndex(roots=("",), by_dotted={}, first_party_top_levels=frozenset()),
        conftest_facts={},
        graph=graph,
    )
    return SelectResult(
        decision=decision,
        outcome=PolicyOutcome(findings=(), waived=(waived,)),
        head=snapshot,
        changed=changed,
        changed_kinds={"src/a.py": NodeKind.MODULE},
        base_sha="b" * 40,
        head_sha="h" * 40,
        tree_fingerprint="f" * 64,
    )


def test_build_report_full_shape() -> None:
    report = build_report(_result(), created_at="2026-01-01T00:00:00+00:00", durations=None)

    assert report["schema"] == REPORT_SCHEMA
    assert report["tool"]["name"] == "acquit"
    assert report["tool"]["graph_schema"] == 2
    assert report["run"] == {
        "base_sha": "b" * 40,
        "head_sha": "h" * 40,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    assert report["graph"] == {"hash": "hand-built", "nodes": 4, "edges": 2, "roots": [""]}
    assert report["changed"] == [{"path": "src/a.py", "kind": "module", "status": "modified"}]
    assert report["decision"]["mode"] == "selective"
    assert report["decision"]["findings"] == []
    assert report["decision"]["waivers"] == [
        {"rule": "R001", "glob": "data.*", "justification": "fixture data", "subject": "data.csv"}
    ]
    assert report["tests"]["selected"] == [
        {"path": "tests/test_a.py", "reasons": ["reachable-from:src/a.py"]}
    ]
    assert report["tests"]["skipped"] == [{"path": "tests/test_b.py", "witness": "w-000001"}]
    assert report["tests"]["always_run"] == []
    assert report["stats"] == {
        "selected": 1,
        "skipped": 1,
        "always_run": 0,
        "total": 2,
        "estimated_seconds_saved": None,
        "durations_source": None,
    }


def test_build_report_sums_durations_over_skipped_tests() -> None:
    durations = {"tests/test_a.py": 9.0, "tests/test_b.py": 2.5}
    report = build_report(_result(), created_at="2026-01-01T00:00:00+00:00", durations=durations)

    assert report["stats"]["estimated_seconds_saved"] == 2.5
    assert report["stats"]["durations_source"] == "durations-file"


def test_build_witnesses_doc_shape() -> None:
    result = _result()
    document = build_witnesses_doc(result.decision, "hand-built")

    assert document["schema"] == WITNESSES_SCHEMA
    assert document["graph_hash"] == "hand-built"
    (witness,) = document["witnesses"]
    expected_closure = ["src/b.py", "tests/test_b.py"]
    assert witness == {
        "id": "w-000001",
        "test": "tests/test_b.py",
        "closure": closure_hash(expected_closure),
        "changed": ["src/a.py"],
        "claim": "closure(test) does not intersect changed set",
    }
    assert document["closures"] == {closure_hash(expected_closure): expected_closure}


def _narrowed_decision() -> Decision:
    narrowed = (
        NarrowedFile(
            path="src/a.py",
            base_blob="b" * 40,
            head_blob="h" * 40,
            inits=(ReliedInit(path="src/__init__.py", base_tier="strict", head_tier="strict"),),
        ),
    )
    closure = ["src/a.py", "tests/test_b.py"]
    witness = build_witness(1, "tests/test_b.py", closure, ["src/a.py"], narrowed)
    return Decision(
        mode=SelectionMode.SELECTIVE,
        selected=(),
        skipped=(SkippedTest(path="tests/test_b.py", witness_id="w-000001", narrowed=True),),
        always_run=(),
        witnesses=(witness,),
        closures={witness.closure_hash: tuple(closure)},
    )


def test_witnesses_doc_serializes_the_narrowed_block_in_the_existing_shape() -> None:
    decision = _narrowed_decision()
    document = build_witnesses_doc(decision, "hand-built")

    assert document["schema"] == WITNESSES_SCHEMA
    (entry,) = document["witnesses"]
    assert entry == {
        "id": "w-000001",
        "test": "tests/test_b.py",
        "closure": closure_hash(["src/a.py", "tests/test_b.py"]),
        "changed": ["src/a.py"],
        "claim": CLAIM_NARROWED,
        "narrowed": [
            {
                "path": "src/a.py",
                "base_blob": "b" * 40,
                "head_blob": "h" * 40,
                "inits": [
                    {"path": "src/__init__.py", "base_tier": "strict", "head_tier": "strict"}
                ],
            }
        ],
    }


def test_report_marks_narrowed_skips_and_selection_doc_is_unchanged() -> None:
    result = replace(_result(), decision=_narrowed_decision())
    report = build_report(result, created_at="2026-01-01T00:00:00+00:00", durations=None)
    assert report["tests"]["skipped"] == [
        {"path": "tests/test_b.py", "witness": "w-000001", "narrowed": True}
    ]

    selection = build_selection_doc(result.decision, "hand-built", "h" * 40, "f" * 64)
    assert selection["skip"] == [{"path": "tests/test_b.py", "witness": "w-000001"}]


def test_build_selection_doc_binds_skips_to_the_analyzed_tree() -> None:
    result = _result()
    document = build_selection_doc(result.decision, "hand-built", "h" * 40, "f" * 64)

    assert document["schema"] == SELECTION_SCHEMA
    assert document["mode"] == "selective"
    assert document["graph_hash"] == "hand-built"
    assert document["tree"] == {"head_sha": "h" * 40, "fingerprint": "f" * 64}
    assert document["artifacts"] == {"report": None, "selection": None, "witnesses": None}
    assert document["skip"] == [{"path": "tests/test_b.py", "witness": "w-000001"}]


def test_build_selection_doc_records_in_repo_artifacts() -> None:
    result = _result()
    artifacts: dict[str, str | None] = {
        "report": "out/acquit-report.json",
        "selection": None,
        "witnesses": "out/acquit-witnesses.json",
    }
    document = build_selection_doc(result.decision, "hand-built", None, "f" * 64, artifacts)

    assert document["artifacts"] == artifacts


def test_run_all_report_shape() -> None:
    run = RunInfo(base_sha="abc", head_sha="def", created_at="2026-01-01T00:00:00+00:00")
    report = build_run_all_report(run, findings=[FINDING])

    assert report["schema"] == REPORT_SCHEMA
    assert report["decision"]["mode"] == "run-all"
    assert report["decision"]["findings"][0]["rule"] == "R018"
    assert report["tests"]["skipped"] == []
    assert report["graph"] == {"hash": None, "nodes": 0, "edges": 0, "roots": []}
    assert report["changed"] == []
    assert report["stats"]["total"] == 0
    assert report["stats"]["estimated_seconds_saved"] is None


def test_run_all_selection_binds_nothing_and_skips_nothing() -> None:
    selection = build_run_all_selection()
    assert selection == {
        "schema": SELECTION_SCHEMA,
        "mode": "run-all",
        "graph_hash": None,
        "tree": {"head_sha": None, "fingerprint": None},
        "skip": [],
    }


def test_canonical_json_is_deterministic() -> None:
    document = {"b": 1, "a": {"d": 2, "c": 3}}
    first = to_canonical_json(document)
    second = to_canonical_json({"a": {"c": 3, "d": 2}, "b": 1})
    assert first == second
    assert first.endswith("\n")
