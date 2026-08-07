"""Fold per-PR study results into the headline numbers.

Reads every result and exclusion file under a directory tree (shard artifacts
land in subdirectories), computes the summary, and writes a markdown page
plus a machine-readable summary json. The README study table is generated
from that json by this command and is never hand-edited.

Percentiles use linear interpolation between closest ranks, the same method
as statistics.quantiles with method="inclusive": for n values sorted
ascending, percentile q sits at position (n - 1) * q, interpolating between
the two neighbouring values when the position is fractional.
"""

import json
import math
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from acquit.errors import AcquitError
from acquit.report import to_canonical_json
from acquit.study import EXCLUSION_SCHEMA, RESULT_SCHEMA, SUMMARY_SCHEMA

# The one blocker a repo config can neutralize: R001 flags changed resource
# files, and an assume_inert glob vouches that the named files feed no test.
_RECOVERABLE_RULE: Final = "R001"


@dataclass(frozen=True, slots=True)
class PrResult:
    """The slice of one per-PR result file that aggregation needs."""

    number: int
    mode: str
    selected: int
    skipped: int
    always_run: int
    total: int
    findings: tuple[tuple[str, str], ...]
    skip_paths: tuple[str, ...]
    unsafe_skips: tuple[str, ...]
    new_tests_selected: bool
    replay_verified: bool
    analysis_seconds: float
    per_file_durations: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ExclusionRecord:
    number: int
    stage: str


@dataclass(frozen=True, slots=True)
class Quartiles:
    p25: float
    median: float
    p75: float


@dataclass(frozen=True, slots=True)
class StudySummary:
    """Every number the summary page and the README table quote."""

    analyzed: int
    excluded: int
    exclusion_stages: Mapping[str, int]
    selective_count: int
    selective_share: float
    skip_rate_count_weighted: Quartiles | None
    skip_rate_duration_weighted: Quartiles | None
    fail_closed_rules: Mapping[str, int]
    sole_blocker_rules: Mapping[str, int]
    recoverable_run_alls: int
    counterfactual_selective_share: float
    unsafe_skips_total: int
    new_test_violations: int
    replay_selective: int
    replay_selective_verified: int
    analysis_p50: float | None
    analysis_p95: float | None


def percentile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks; documented in the module."""
    if not values:
        raise AcquitError("percentile of an empty series")
    if not 0.0 <= q <= 1.0:
        raise AcquitError(f"percentile q must be within [0, 1], got {q}")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _quartiles(values: Sequence[float]) -> Quartiles | None:
    if not values:
        return None
    return Quartiles(
        p25=percentile(values, 0.25),
        median=percentile(values, 0.5),
        p75=percentile(values, 0.75),
    )


def _int_field(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float_field(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _str_tuple(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(entry for entry in value if isinstance(entry, str))


def _findings(data: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    value = data.get("findings")
    if not isinstance(value, list):
        return ()
    pairs: list[tuple[str, str]] = []
    for entry in value:
        if isinstance(entry, Mapping):
            pairs.append((str(entry.get("rule", "?")), str(entry.get("scope", "?"))))
    return tuple(pairs)


def _durations(data: Mapping[str, Any]) -> Mapping[str, float]:
    value = data.get("per_file_durations")
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, raw in value.items():
        if isinstance(key, str) and isinstance(raw, int | float) and not isinstance(raw, bool):
            out[key] = float(raw)
    return out


def _pr_result(data: Mapping[str, Any]) -> PrResult:
    return PrResult(
        number=_int_field(data, "number"),
        mode=str(data.get("mode", "run-all")),
        selected=_int_field(data, "selected"),
        skipped=_int_field(data, "skipped"),
        always_run=_int_field(data, "always_run"),
        total=_int_field(data, "total"),
        findings=_findings(data),
        skip_paths=_str_tuple(data, "skip_paths"),
        unsafe_skips=_str_tuple(data, "unsafe_skips"),
        new_tests_selected=data.get("new_tests_selected") is not False,
        replay_verified=data.get("replay_verified") is True,
        analysis_seconds=_float_field(data, "analysis_seconds"),
        per_file_durations=_durations(data),
    )


def load_results(results_dir: Path) -> tuple[tuple[PrResult, ...], tuple[ExclusionRecord, ...]]:
    """Collect result and exclusion files anywhere under the directory.

    Files with other schemas (the captured report/selection/witness documents
    share the pr- prefix) are skipped. A PR that has both an exclusion and a
    result, from a retry that later succeeded, counts as analyzed.
    """
    results: dict[int, PrResult] = {}
    exclusions: dict[int, str] = {}
    for path in sorted(results_dir.rglob("pr-*.json")):
        data = _read_json(path)
        if data is None or data.get("schema") != RESULT_SCHEMA:
            continue
        result = _pr_result(data)
        results[result.number] = result
    for path in sorted(results_dir.rglob("excluded-*.json")):
        data = _read_json(path)
        if data is None or data.get("schema") != EXCLUSION_SCHEMA:
            continue
        exclusions[_int_field(data, "number")] = str(data.get("stage", "unknown"))
    pruned = {number: stage for number, stage in exclusions.items() if number not in results}
    ordered_results = tuple(results[number] for number in sorted(results))
    ordered_exclusions = tuple(
        ExclusionRecord(number=number, stage=pruned[number]) for number in sorted(pruned)
    )
    return ordered_results, ordered_exclusions


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sole_global_blocker(result: PrResult) -> str | None:
    """The single global rule that alone forced this run-all, when provable.

    A run-all is sole-blocked when exactly one distinct rule fired with
    global scope, no global-if-reached finding is present (such a finding may
    have been the one that acted, and the record cannot tell), and the
    recorded totals are zero. Zero totals mean the decision short-circuited
    on the global finding before consulting the graph; a run-all with totals
    walked the graph and every test was impacted anyway, so removing the
    rule would not have made it selective.
    """
    global_rules = {rule for rule, scope in result.findings if scope == "global"}
    if len(global_rules) != 1:
        return None
    if any(scope == "global-if-reached" for _, scope in result.findings):
        return None
    if result.total > 0:
        return None
    return next(iter(global_rules))


def summarize(results: Sequence[PrResult], exclusions: Sequence[ExclusionRecord]) -> StudySummary:
    """Compute the study summary; pure over already-loaded records."""
    selective = [result for result in results if result.mode == "selective"]
    count_rates = [result.skipped / result.total for result in selective if result.total > 0]
    duration_rates: list[float] = []
    for result in selective:
        total_seconds = sum(result.per_file_durations.values())
        if total_seconds <= 0:
            continue
        skipped_seconds = sum(
            result.per_file_durations.get(path, 0.0) for path in result.skip_paths
        )
        duration_rates.append(skipped_seconds / total_seconds)
    rules: Counter[str] = Counter()
    sole_blockers: Counter[str] = Counter()
    for result in results:
        if result.mode == "selective":
            continue
        global_rules = sorted({rule for rule, scope in result.findings if scope == "global"})
        if global_rules:
            rules.update(global_rules)
        else:
            # No rule fired; the diff genuinely reaches every test file.
            rules["full-graph-impact"] += 1
        blocker = sole_global_blocker(result)
        if blocker is not None:
            sole_blockers[blocker] += 1
    recoverable = sole_blockers.get(_RECOVERABLE_RULE, 0)
    analysis = [result.analysis_seconds for result in results]
    return StudySummary(
        analyzed=len(results),
        excluded=len(exclusions),
        exclusion_stages=dict(sorted(Counter(entry.stage for entry in exclusions).items())),
        selective_count=len(selective),
        selective_share=len(selective) / len(results) if results else 0.0,
        skip_rate_count_weighted=_quartiles(count_rates),
        skip_rate_duration_weighted=_quartiles(duration_rates),
        fail_closed_rules=dict(sorted(rules.items())),
        sole_blocker_rules=dict(sorted(sole_blockers.items())),
        recoverable_run_alls=recoverable,
        counterfactual_selective_share=(
            (len(selective) + recoverable) / len(results) if results else 0.0
        ),
        unsafe_skips_total=sum(len(result.unsafe_skips) for result in results),
        new_test_violations=sum(1 for result in results if not result.new_tests_selected),
        replay_selective=len(selective),
        replay_selective_verified=sum(1 for result in selective if result.replay_verified),
        analysis_p50=percentile(analysis, 0.5) if analysis else None,
        analysis_p95=percentile(analysis, 0.95) if analysis else None,
    )


def _quartiles_dict(quartiles: Quartiles | None) -> dict[str, float] | None:
    if quartiles is None:
        return None
    return {"p25": quartiles.p25, "median": quartiles.median, "p75": quartiles.p75}


def summary_to_dict(summary: StudySummary) -> dict[str, Any]:
    return {
        "schema": SUMMARY_SCHEMA,
        "prs": {
            "analyzed": summary.analyzed,
            "excluded": summary.excluded,
            "exclusion_stages": dict(summary.exclusion_stages),
        },
        "selective": {"count": summary.selective_count, "share": summary.selective_share},
        "skip_rate_count_weighted": _quartiles_dict(summary.skip_rate_count_weighted),
        "skip_rate_duration_weighted": _quartiles_dict(summary.skip_rate_duration_weighted),
        "fail_closed_rules": dict(summary.fail_closed_rules),
        "sole_blocker_rules": dict(summary.sole_blocker_rules),
        "recoverable_run_alls": {
            "count": summary.recoverable_run_alls,
            "counterfactual_selective_share": summary.counterfactual_selective_share,
        },
        "unsafe_skips_total": summary.unsafe_skips_total,
        "new_test_violations": summary.new_test_violations,
        "replay": {
            "selective": summary.replay_selective,
            "selective_verified": summary.replay_selective_verified,
        },
        "analysis_seconds": {"p50": summary.analysis_p50, "p95": summary.analysis_p95},
    }


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _median_cell(quartiles: Quartiles | None) -> str:
    return "-" if quartiles is None else _pct(quartiles.median)


def _quartile_rows(label: str, quartiles: Quartiles | None) -> list[str]:
    if quartiles is None:
        return [f"| {label} | - | - | - |"]
    return [
        f"| {label} | {_pct(quartiles.p25)} | {_pct(quartiles.median)} | {_pct(quartiles.p75)} |"
    ]


def render_markdown(summary: StudySummary) -> str:
    """Render the summary page; the headline table is the README study table."""
    replay_cell = f"{summary.replay_selective_verified}/{summary.replay_selective} verified"
    lines = [
        "# Acquit replay study",
        "",
        "Generated by `acquit-study aggregate` from per-PR result files.",
        "Regenerate it after any run; never edit it by hand.",
        "",
        "## Headline",
        "",
        "| PRs analyzed | Excluded | Selective | Median skip (files) | Median skip (time) |"
        " Unsafe skips | Replay |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        f"| {summary.analyzed} | {summary.excluded} "
        f"| {summary.selective_count} ({_pct(summary.selective_share)}) "
        f"| {_median_cell(summary.skip_rate_count_weighted)} "
        f"| {_median_cell(summary.skip_rate_duration_weighted)} "
        f"| {summary.unsafe_skips_total} | {replay_cell} |",
        "",
        "## Skip rates over selective PRs",
        "",
        "| Weighting | p25 | median | p75 |",
        "| --- | --- | --- | --- |",
        *_quartile_rows("by file count", summary.skip_rate_count_weighted),
        *_quartile_rows("by base-run duration", summary.skip_rate_duration_weighted),
        "",
        "## Fail-closed reasons on run-all PRs",
        "",
        "| Rule | PRs |",
        "| --- | --- |",
    ]
    for rule, count in summary.fail_closed_rules.items():
        lines.append(f"| {rule} | {count} |")
    if not summary.fail_closed_rules:
        lines.append("| (none) | 0 |")
    lines += [
        "",
        "## Recoverable run-alls",
        "",
        f"- Run-alls whose only global blocker is {_RECOVERABLE_RULE}: "
        f"{summary.recoverable_run_alls}",
        f"- Counterfactual selective share ((selective + recoverable) / analyzed): "
        f"{_pct(summary.counterfactual_selective_share)}",
        "- These PRs would have been selective with an `assume_inert` list under"
        " `[tool.acquit]`, which vouches that the flagged docs or data files feed"
        " no test and removes R001 for exactly those files.",
        "",
        "## Safety",
        "",
        f"- Unsafe skips: {summary.unsafe_skips_total} (must be 0)",
        f"- PRs where a new test was skipped: {summary.new_test_violations} (must be 0)",
        f"- Replay verification: {replay_cell}",
        "",
        "## Analysis overhead",
        "",
    ]
    if summary.analysis_p50 is None or summary.analysis_p95 is None:
        lines.append("No analyzed PRs.")
    else:
        lines.append(
            f"acquit select wall time: p50 {summary.analysis_p50:.2f}s, "
            f"p95 {summary.analysis_p95:.2f}s per PR."
        )
    lines += ["", "## Exclusions", "", "| Stage | PRs |", "| --- | --- |"]
    for stage, count in summary.exclusion_stages.items():
        lines.append(f"| {stage} | {count} |")
    if not summary.exclusion_stages:
        lines.append("| (none) | 0 |")
    return "\n".join(lines) + "\n"


def run_aggregate(results_dir: Path, out_markdown: Path) -> int:
    """Aggregate a results tree; nonzero when the safety numbers are not clean."""
    results, exclusions = load_results(results_dir)
    if not results and not exclusions:
        raise AcquitError(f"no study results under {results_dir}")
    summary = summarize(results, exclusions)
    out_markdown.parent.mkdir(parents=True, exist_ok=True)
    out_json = out_markdown.with_suffix(".json")
    out_json.write_text(to_canonical_json(summary_to_dict(summary)), encoding="utf-8")
    out_markdown.write_text(render_markdown(summary), encoding="utf-8")
    print(f"study: wrote {out_markdown} and {out_json}")
    if summary.unsafe_skips_total or summary.new_test_violations:
        print(
            f"study: FAILED: {summary.unsafe_skips_total} unsafe skip(s), "
            f"{summary.new_test_violations} pr(s) with a skipped new test",
            file=sys.stderr,
        )
        return 1
    return 0
