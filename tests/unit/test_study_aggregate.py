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
    assert summary.unsafe_skips_total == 0
    assert summary.new_test_violations == 0
    assert summary.replay_selective == 4
    assert summary.replay_selective_verified == 4
    assert summary.analysis_p50 == pytest.approx(3.5)
    assert summary.analysis_p95 == pytest.approx(5.75)


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
    markdown = out.read_text(encoding="utf-8")
    assert "Unsafe skips: 0 (must be 0)" in markdown
    assert "4/4 verified" in markdown
    assert "| R001 | 1 |" in markdown


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
