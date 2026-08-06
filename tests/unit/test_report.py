from acquit.constants import REPORT_SCHEMA, SELECTION_SCHEMA
from acquit.policy.model import Finding, RuleId, Scope, ScopeKind
from acquit.report import (
    RunInfo,
    SelectionMode,
    build_run_all_report,
    build_selection,
    to_canonical_json,
)

FINDING = Finding(
    rule=RuleId.INTERNAL_ERROR,
    scope=Scope(kind=ScopeKind.GLOBAL),
    subject="acquit",
    reason="test reason",
)


def test_run_all_report_shape() -> None:
    run = RunInfo(base_sha="abc", head_sha="def", created_at="2026-01-01T00:00:00+00:00")
    report = build_run_all_report(run, findings=[FINDING])

    assert report["schema"] == REPORT_SCHEMA
    assert report["decision"]["mode"] == "run-all"
    assert report["decision"]["findings"][0]["rule"] == "R018"
    assert report["tests"]["skipped"] == []
    assert report["stats"]["total"] == 0


def test_selection_sorts_skip_list() -> None:
    selection = build_selection(SelectionMode.SELECTIVE, skip=["b.py", "a.py"], graph_hash="x")
    assert selection["schema"] == SELECTION_SCHEMA
    assert selection["skip"] == ["a.py", "b.py"]


def test_canonical_json_is_deterministic() -> None:
    document = {"b": 1, "a": {"d": 2, "c": 3}}
    first = to_canonical_json(document)
    second = to_canonical_json({"a": {"c": 3, "d": 2}, "b": 1})
    assert first == second
    assert first.endswith("\n")
