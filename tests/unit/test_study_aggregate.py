"""Aggregation: documented percentiles, histograms, and the safety gate."""

import json
from pathlib import Path
from typing import Any

import pytest

from acquit.errors import AcquitError
from acquit.study import EXCLUSION_SCHEMA, RESULT_SCHEMA
from acquit.study.aggregate import (
    load_results,
    percentile,
    render_markdown,
    run_aggregate,
    summarize,
)


def _result(number: int, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "number": number,
        "base_sha": f"base{number}",
        "head_sha": f"head{number}",
        "mode": "selective",
        "selected": 1,
        "skipped": 0,
        "always_run": 1,
        "total": 10,
        "findings": [],
        "skip_paths": [],
        "changed_outcomes": [],
        "unsafe_skips": [],
        "new_tests_selected": True,
        "replay_verified": True,
        "analysis_seconds": 1.0,
        "base_suite_seconds": 60.0,
        "head_suite_seconds": 61.0,
        "per_file_durations": {},
    }
    payload.update(overrides)
    return payload


_R001 = {"rule": "R001", "scope": "global", "subject": "CHANGES.md"}
_R002 = {"rule": "R002", "scope": "global", "subject": "pyproject.toml"}
_R007 = {"rule": "R007", "scope": "closure-taint", "subject": "pkg/dynamic.py"}


def _run_all(number: int, findings: list[dict[str, str]], **overrides: Any) -> dict[str, Any]:
    """A run-all that short-circuited on a global finding: totals stay zero."""
    return _result(
        number,
        mode="run-all",
        selected=0,
        skipped=0,
        always_run=0,
        total=0,
        findings=findings,
        **overrides,
    )


def _full_impact(number: int, findings: list[dict[str, str]]) -> dict[str, Any]:
    """A run-all where the graph selected every test: the diff reaches everything."""
    return _result(
        number,
        mode="run-all",
        selected=10,
        skipped=0,
        always_run=0,
        total=10,
        findings=findings,
    )


def _selective(number: int, skipped: int, analysis: float) -> dict[str, Any]:
    keep_seconds = float(10 - skipped)
    return _result(
        number,
        skipped=skipped,
        selected=10 - skipped,
        skip_paths=["tests/skip.py"],
        per_file_durations={"tests/keep.py": keep_seconds, "tests/skip.py": float(skipped)},
        analysis_seconds=analysis,
    )


def _write(directory: Path, name: str, payload: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def _fixture_tree(root: Path) -> None:
    shard_a, shard_b = root / "shard-a", root / "shard-b"
    _write(shard_a, "pr-000001.json", _selective(1, 2, 1.0))
    _write(shard_a, "pr-000002.json", _selective(2, 4, 2.0))
    _write(shard_b, "pr-000003.json", _selective(3, 6, 3.0))
    _write(shard_b, "pr-000004.json", _selective(4, 8, 4.0))
    _write(
        shard_a,
        "pr-000005.json",
        _result(
            5,
            mode="run-all",
            findings=[
                {"rule": "R001", "scope": "global", "subject": "CHANGES.md"},
                {"rule": "R002", "scope": "global", "subject": "pyproject.toml"},
                {"rule": "R007", "scope": "closure-taint", "subject": "x.py"},
            ],
            analysis_seconds=5.0,
        ),
    )
    _write(shard_b, "pr-000006.json", _result(6, mode="run-all", analysis_seconds=6.0))
    # Captured acquit documents share the pr- prefix and must be ignored.
    _write(shard_a, "pr-000001-report.json", {"schema": "acquit/report-v1"})
    _write(
        shard_a,
        "excluded-000007.json",
        {
            "schema": EXCLUSION_SCHEMA,
            "number": 7,
            "stage": "base-suite",
            "reason": "base-suite: pytest exited with 2",
        },
    )


def test_percentile_matches_documented_interpolation() -> None:
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert percentile([1, 2], 0.95) == pytest.approx(1.95)
    assert percentile([0.2, 0.4, 0.6, 0.8], 0.25) == pytest.approx(0.35)
    assert percentile([0.2, 0.4, 0.6, 0.8], 0.75) == pytest.approx(0.65)
    assert percentile([7.0], 0.95) == 7.0


def test_percentile_rejects_empty_and_bad_q() -> None:
    with pytest.raises(AcquitError):
        percentile([], 0.5)
    with pytest.raises(AcquitError):
        percentile([1.0], 1.5)


def test_summarize_computes_documented_numbers(tmp_path: Path) -> None:
    _fixture_tree(tmp_path)
    results, exclusions = load_results(tmp_path)
    summary = summarize(results, exclusions)
    assert summary.analyzed == 6
    assert summary.excluded == 1
    assert summary.exclusion_stages == {"base-suite": 1}
    assert summary.selective_count == 4
    assert summary.selective_share == pytest.approx(4 / 6)
    count_rates = summary.skip_rate_count_weighted
    assert count_rates is not None
    assert count_rates.p25 == pytest.approx(0.35)
    assert count_rates.median == pytest.approx(0.5)
    assert count_rates.p75 == pytest.approx(0.65)
    duration_rates = summary.skip_rate_duration_weighted
    assert duration_rates is not None
    assert duration_rates.median == pytest.approx(0.5)
    assert summary.fail_closed_rules == {"R001": 1, "R002": 1, "full-graph-impact": 1}
    assert summary.sole_blocker_rules == {}
    assert summary.recoverable_run_alls == 0
    assert summary.counterfactual_selective_share == pytest.approx(4 / 6)
    assert summary.unsafe_skips_total == 0
    assert summary.new_test_violations == 0
    assert summary.replay_selective == 4
    assert summary.replay_selective_verified == 4
    assert summary.analysis_p50 == pytest.approx(3.5)
    assert summary.analysis_p95 == pytest.approx(5.75)


def test_sole_blocker_r001_only_is_recoverable(tmp_path: Path) -> None:
    _write(tmp_path, "pr-000001.json", _selective(1, 2, 1.0))
    # R001 is the only global finding; non-global findings do not disqualify.
    _write(tmp_path, "pr-000002.json", _run_all(2, [_R001, _R007]))
    results, exclusions = load_results(tmp_path)
    summary = summarize(results, exclusions)
    assert summary.sole_blocker_rules == {"R001": 1}
    assert summary.recoverable_run_alls == 1
    assert summary.counterfactual_selective_share == pytest.approx(2 / 2)


def test_sole_blocker_rejects_second_global_rule(tmp_path: Path) -> None:
    _write(tmp_path, "pr-000001.json", _selective(1, 2, 1.0))
    _write(tmp_path, "pr-000002.json", _run_all(2, [_R001, _R002]))
    results, exclusions = load_results(tmp_path)
    summary = summarize(results, exclusions)
    assert summary.sole_blocker_rules == {}
    assert summary.recoverable_run_alls == 0
    assert summary.counterfactual_selective_share == pytest.approx(1 / 2)


def test_sole_blocker_rejects_r001_with_full_graph_impact(tmp_path: Path) -> None:
    # R001 fired, but the recorded totals show the graph selected every test
    # anyway, so removing R001 would not have made the PR selective.
    _write(tmp_path, "pr-000001.json", _selective(1, 2, 1.0))
    _write(tmp_path, "pr-000002.json", _full_impact(2, [_R001]))
    results, exclusions = load_results(tmp_path)
    summary = summarize(results, exclusions)
    assert summary.sole_blocker_rules == {}
    assert summary.recoverable_run_alls == 0
    assert summary.counterfactual_selective_share == pytest.approx(1 / 2)


def test_sole_blocker_rejects_full_graph_impact_without_findings(tmp_path: Path) -> None:
    _write(tmp_path, "pr-000001.json", _selective(1, 2, 1.0))
    _write(tmp_path, "pr-000002.json", _full_impact(2, []))
    results, exclusions = load_results(tmp_path)
    summary = summarize(results, exclusions)
    assert summary.sole_blocker_rules == {}
    assert summary.recoverable_run_alls == 0
    assert summary.counterfactual_selective_share == pytest.approx(1 / 2)


def test_sole_blocker_counts_other_rules_without_recovery(tmp_path: Path) -> None:
    # A sole R002 blocker is counted per rule, but only R001 is recoverable,
    # and an ambient global-if-reached finding fails closed: it may have been
    # the finding that acted.
    _write(
        tmp_path,
        "pr-000001.json",
        _run_all(1, [{"rule": "R002", "scope": "global", "subject": "pyproject.toml"}]),
    )
    _write(
        tmp_path,
        "pr-000002.json",
        _run_all(2, [_R001, {"rule": "R008", "scope": "global-if-reached", "subject": "conf.py"}]),
    )
    results, exclusions = load_results(tmp_path)
    summary = summarize(results, exclusions)
    assert summary.sole_blocker_rules == {"R002": 1}
    assert summary.recoverable_run_alls == 0
    assert summary.counterfactual_selective_share == pytest.approx(0.0)


def test_duplicate_results_count_once(tmp_path: Path) -> None:
    _write(tmp_path / "shard-a", "pr-000001.json", _selective(1, 2, 1.0))
    _write(tmp_path / "shard-b", "pr-000001.json", _selective(1, 2, 1.0))
    results, _ = load_results(tmp_path)
    assert len(results) == 1


def test_result_supersedes_exclusion(tmp_path: Path) -> None:
    _write(tmp_path, "pr-000003.json", _selective(3, 2, 1.0))
    _write(
        tmp_path,
        "excluded-000003.json",
        {
            "schema": EXCLUSION_SCHEMA,
            "number": 3,
            "stage": "fetch",
            "reason": "fetch: flake",
        },
    )
    results, exclusions = load_results(tmp_path)
    assert [result.number for result in results] == [3]
    assert exclusions == ()


def test_run_aggregate_writes_summary_and_passes_when_clean(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _fixture_tree(results_dir)
    out = tmp_path / "flask-summary.md"
    assert run_aggregate(results_dir, out) == 0
    summary = json.loads((tmp_path / "flask-summary.json").read_text(encoding="utf-8"))
    assert summary["unsafe_skips_total"] == 0
    assert summary["prs"] == {
        "analyzed": 6,
        "excluded": 1,
        "exclusion_stages": {"base-suite": 1},
    }
    assert summary["sole_blocker_rules"] == {}
    assert summary["recoverable_run_alls"] == {
        "count": 0,
        "counterfactual_selective_share": pytest.approx(4 / 6),
    }
    markdown = out.read_text(encoding="utf-8")
    assert "Unsafe skips: 0 (must be 0)" in markdown
    assert "4/4 verified" in markdown
    assert "| R001 | 1 |" in markdown
    assert "## Recoverable run-alls" in markdown
    assert "Run-alls whose only global blocker is R001: 0" in markdown


def test_run_aggregate_fails_on_unsafe_skip(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write(
        results_dir,
        "pr-000009.json",
        _result(9, unsafe_skips=["tests/test_a.py"], skip_paths=["tests/test_a.py"], skipped=1),
    )
    assert run_aggregate(results_dir, tmp_path / "s.md") == 1


def test_run_aggregate_fails_on_skipped_new_test(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write(results_dir, "pr-000009.json", _result(9, new_tests_selected=False))
    assert run_aggregate(results_dir, tmp_path / "s.md") == 1


def test_run_aggregate_rejects_empty_tree(tmp_path: Path) -> None:
    with pytest.raises(AcquitError):
        run_aggregate(tmp_path, tmp_path / "s.md")


def test_markdown_handles_no_selective_prs() -> None:
    summary = summarize([], [])
    rendered = render_markdown(summary)
    assert "| by file count | - | - | - |" in rendered
    assert "No analyzed PRs." in rendered
    assert "Run-alls whose only global blocker is R001: 0" in rendered


def test_no_narrowing_section_without_flagged_prs(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write(results_dir, "pr-000001.json", _selective(1, 2, 1.0))
    out = tmp_path / "summary.md"
    assert run_aggregate(results_dir, out) == 0
    assert "## Narrowing" not in out.read_text(encoding="utf-8")
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["narrowing"] == {
        "prs_with_flag": 0,
        "prs_with_narrowed_skips": 0,
        "narrowed_skips": 0,
        "unsafe": [],
        "refusals": {},
    }


def test_narrowing_section_sums_counts_and_refusals(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pr-000001.json",
        _result(
            1,
            narrowing=True,
            narrowed_skips=2,
            skipped=3,
            selected=7,
            narrowing_refusals={"impure-init": 2},
        ),
    )
    _write(
        tmp_path,
        "pr-000002.json",
        _result(
            2,
            narrowing=True,
            narrowing_refusals={"impure-init": 1, "not-import-inert": 4},
        ),
    )
    _write(tmp_path, "pr-000003.json", _selective(3, 2, 1.0))
    results, exclusions = load_results(tmp_path)
    summary = summarize(results, exclusions)
    assert summary.narrowing_prs == 2
    assert summary.narrowed_skip_prs == 1
    assert summary.narrowed_skips_total == 2
    assert summary.narrowed_unsafe == ()
    assert summary.narrowing_refusals == {"impure-init": 3, "not-import-inert": 4}
    rendered = render_markdown(summary)
    assert "## Narrowing" in rendered
    assert "- PRs run with narrowing enabled: 2" in rendered
    assert "- PRs with narrowed skips: 1" in rendered
    assert "- Narrowed skips: 2" in rendered
    assert "- Unsafe among them: 0 (must be 0)" in rendered
    assert "| impure-init | 3 |" in rendered
    assert "| not-import-inert | 4 |" in rendered


def test_narrowing_section_renders_empty_refusal_placeholder(tmp_path: Path) -> None:
    _write(tmp_path, "pr-000001.json", _result(1, narrowing=True))
    results, exclusions = load_results(tmp_path)
    rendered = render_markdown(summarize(results, exclusions))
    assert "## Narrowing" in rendered
    assert "| (none) | 0 |" in rendered


def test_narrowed_unsafe_skip_is_listed_and_fatal(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write(
        results_dir,
        "pr-000009.json",
        _result(
            9,
            narrowing=True,
            narrowed_skips=1,
            skipped=1,
            skip_paths=["tests/test_a.py"],
            unsafe_skips=["tests/test_a.py"],
            unsafe_narrowed_skips=["tests/test_a.py"],
        ),
    )
    out = tmp_path / "summary.md"
    assert run_aggregate(results_dir, out) == 1
    markdown = out.read_text(encoding="utf-8")
    assert "- Unsafe among them: 1 (must be 0)" in markdown
    assert "| 9 | tests/test_a.py |" in markdown
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["narrowing"]["unsafe"] == [{"pr": 9, "path": "tests/test_a.py"}]
