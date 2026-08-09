"""Standing-hazard census across many open-source pytest repositories.

The replay study measures acquit on real diffs; the census measures what it
is up against before any diff exists. Each repository is shallow-cloned and
snapshotted through acquit's own pipeline (static analysis only, the target's
code never runs), and the standing hazards are inventoried: dynamic idioms
that taint modules, conftests that can alter collection, doctest
configuration, and fat top-level __init__.py files that put most of the suite
inside every test closure. The aggregate ranks which sound resolver would buy
back the most selectivity across the corpus.
"""

import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import rustworkx as rx

from acquit.config import load_config
from acquit.errors import AcquitError
from acquit.graph.model import BuiltGraph, EdgeKind, Node, NodeKind
from acquit.graph.parse import SuspectKind
from acquit.pipeline import Snapshot, snapshot_tree
from acquit.pytestmap.pytestcfg import PytestConfig, load_pytest_config
from acquit.report import to_canonical_json
from acquit.select import impacted_tests, tainted_reachers
from acquit.study.aggregate import percentile

CENSUS_REPO_SCHEMA: Final = "acquit/census-repo-v1"
CENSUS_SUMMARY_SCHEMA: Final = "acquit/census-summary-v1"

_SLUG_PATTERN: Final = re.compile(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")
_CLONE_TIMEOUT_SECONDS: Final = 900.0
_TOP_OFFENDERS: Final = 5
# A top-level __init__.py that pins at least this share of tests is "fat".
FAT_INIT_THRESHOLD: Final = 0.5
# Global rules fire on every test in the repo, so their blast radius is 1.
_GLOBAL_COST: Final = 1.0

R006_LABEL: Final = "collection-altering-conftest (R006)"
R015_LABEL: Final = "doctest-modules (R015)"
FAT_INIT_LABEL: Final = "fat-init full-graph exposure"

# Payload for the temporary source behind multi-test forward reachability.
_SOURCE: Final = Node(path="", kind=NodeKind.EXTERNAL)


@dataclass(frozen=True, slots=True)
class KindCensus:
    """One suspect kind's standing footprint inside a single repository."""

    files: int
    occurrences: int
    reached_files: int
    # Share of tests that reach some carrier of this kind; None without tests.
    blast_radius: float | None
    top_offenders: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class RepoCensus:
    """Everything the census records about one successfully snapshotted repo."""

    slug: str
    files: int
    python_files: int
    modules: int
    test_files: int
    conftests: int
    unparseable: int
    graph_nodes: int
    graph_edges: int
    suspects: Mapping[str, KindCensus]
    tainted_reachers: int
    taint_blast_radius: float | None
    r006_files: tuple[str, ...]
    doctest_modules: bool
    doctest_source: str | None
    fat_inits: tuple[tuple[str, float], ...]
    fat_init_max: float | None
    suspect_files: int
    unreached_suspect_files: int
    unreached_hazard_share: float | None


@dataclass(frozen=True, slots=True)
class RepoFailure:
    """A repository the census could not analyze, recorded and never hidden."""

    slug: str
    stage: str
    error: str


@dataclass(frozen=True, slots=True)
class IdiomStat:
    """One suspect kind's footprint across the whole corpus."""

    kind: str
    repos_present: int
    repos_reached: int
    present_share: float
    reached_share: float
    median_blast: float | None


@dataclass(frozen=True, slots=True)
class BuildItem:
    """One candidate for the next sound resolver, scored for ranking."""

    label: str
    affected_repos: int
    affected_share: float
    median_cost: float | None
    score: float


@dataclass(frozen=True, slots=True)
class Distribution:
    p25: float
    median: float
    p75: float
    max: float


@dataclass(frozen=True, slots=True)
class CensusSummary:
    """Every number the census summary page quotes."""

    analyzed: int
    failed: int
    zero_test_repos: int
    idioms: tuple[IdiomStat, ...]
    build_next: tuple[BuildItem, ...]
    taint_blast: Distribution | None
    fat_init: Distribution | None
    fat_init_over_half: int
    unreached_share: Distribution | None
    r006_repos: int
    doctest_repos: int
    conftest_median: float | None
    failures: tuple[RepoFailure, ...]


def parse_repos_list(text: str) -> tuple[str, ...]:
    """Parse the repos file: one owner/name per line, # comments, dedupe."""
    slugs: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not _SLUG_PATTERN.fullmatch(line):
            raise AcquitError(f"repos list line {lineno}: expected owner/name, got {line!r}")
        if line not in slugs:
            slugs.append(line)
    return tuple(slugs)


def _reached_from_tests(graph: BuiltGraph, tests: Iterable[str]) -> frozenset[str]:
    """Every path inside some test's import closure, tests included."""
    indices = sorted(graph.index_of[path] for path in set(tests) if path in graph.index_of)
    if not indices:
        return frozenset()
    work = graph.digraph.copy()
    source = work.add_node(_SOURCE)
    for index in indices:
        work.add_edge(source, index, EdgeKind.IMPORTS)
    return frozenset(work[index].path for index in rx.descendants(work, source))


def _top_level_inits(snapshot: Snapshot) -> tuple[str, ...]:
    """The __init__.py of every top-level package under any import root."""
    inits: set[str] = set()
    for dotted, paths in snapshot.index.by_dotted.items():
        if "." in dotted:
            continue
        inits.update(path for path in paths if path.rsplit("/", 1)[-1] == "__init__.py")
    return tuple(sorted(inits))


def census_of_snapshot(slug: str, snapshot: Snapshot, pytest_config: PytestConfig) -> RepoCensus:
    """Compute one repository's standing-hazard census; pure over its inputs."""
    graph = snapshot.graph
    tests = sorted(path for path, node in graph.nodes.items() if node.kind is NodeKind.TEST)
    test_count = len(tests)
    reached = _reached_from_tests(graph, tests)

    per_kind_counts: dict[SuspectKind, Counter[str]] = {kind: Counter() for kind in SuspectKind}
    for path, facts in snapshot.facts.items():
        for suspect in facts.suspects:
            per_kind_counts[suspect.kind][path] += 1

    suspects: dict[str, KindCensus] = {}
    for kind, counter in per_kind_counts.items():
        carriers = sorted(counter)
        blast: float | None = None
        if test_count:
            blast = (len(impacted_tests(graph, carriers)) if carriers else 0) / test_count
        offenders = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        suspects[kind.value] = KindCensus(
            files=len(carriers),
            occurrences=sum(counter.values()),
            reached_files=sum(1 for path in carriers if path in reached),
            blast_radius=blast,
            top_offenders=tuple(offenders[:_TOP_OFFENDERS]),
        )

    suspect_files = sorted({path for counter in per_kind_counts.values() for path in counter})
    unreached = [path for path in suspect_files if path not in reached]

    reacher_count = len(tainted_reachers(graph))
    fat_inits: tuple[tuple[str, float], ...] = ()
    if test_count:
        fat_inits = tuple(
            (init, len(impacted_tests(graph, (init,))) / test_count)
            for init in _top_level_inits(snapshot)
        )

    kinds = snapshot.kinds
    return RepoCensus(
        slug=slug,
        files=len(snapshot.files),
        python_files=sum(1 for path in snapshot.files if path.endswith(".py")),
        modules=sum(1 for kind in kinds.values() if kind is NodeKind.MODULE),
        test_files=test_count,
        conftests=sum(1 for kind in kinds.values() if kind is NodeKind.CONFTEST),
        unparseable=len(snapshot.unparseable),
        graph_nodes=len(graph.nodes),
        graph_edges=graph.digraph.num_edges(),
        suspects=suspects,
        tainted_reachers=reacher_count,
        taint_blast_radius=reacher_count / test_count if test_count else None,
        r006_files=tuple(
            sorted(
                path for path, facts in snapshot.conftest_facts.items() if facts.collection_altering
            )
        ),
        doctest_modules=pytest_config.doctest_modules,
        doctest_source=pytest_config.source if pytest_config.doctest_modules else None,
        fat_inits=fat_inits,
        fat_init_max=max((share for _, share in fat_inits), default=None),
        suspect_files=len(suspect_files),
        unreached_suspect_files=len(unreached),
        unreached_hazard_share=len(unreached) / len(suspect_files) if suspect_files else None,
    )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def repo_census_to_dict(census: RepoCensus) -> dict[str, Any]:
    return {
        "schema": CENSUS_REPO_SCHEMA,
        "slug": census.slug,
        "counts": {
            "files": census.files,
            "python_files": census.python_files,
            "modules": census.modules,
            "test_files": census.test_files,
            "conftests": census.conftests,
            "unparseable": census.unparseable,
            "graph_nodes": census.graph_nodes,
            "graph_edges": census.graph_edges,
        },
        "suspects": {
            kind: {
                "files": entry.files,
                "occurrences": entry.occurrences,
                "reached_files": entry.reached_files,
                "blast_radius": _rounded(entry.blast_radius),
                "top_offenders": [
                    {"path": path, "suspects": count} for path, count in entry.top_offenders
                ],
            }
            for kind, entry in sorted(census.suspects.items())
        },
        "taint": {
            "tainted_reachers": census.tainted_reachers,
            "blast_radius": _rounded(census.taint_blast_radius),
        },
        "standing": {
            "r006_files": list(census.r006_files),
            "doctest_modules": census.doctest_modules,
            "doctest_source": census.doctest_source,
        },
        "fat_init": {
            "per_init": [
                {"path": path, "share": _rounded(share)} for path, share in census.fat_inits
            ],
            "max": _rounded(census.fat_init_max),
        },
        "unreached": {
            "suspect_files": census.suspect_files,
            "unreached_suspect_files": census.unreached_suspect_files,
            "share": _rounded(census.unreached_hazard_share),
        },
    }


def failure_to_dict(failure: RepoFailure) -> dict[str, Any]:
    return {
        "schema": CENSUS_REPO_SCHEMA,
        "slug": failure.slug,
        "error": {"stage": failure.stage, "message": failure.error},
    }


def _share(count: int, analyzed: int) -> float:
    return count / analyzed if analyzed else 0.0


def _distribution(values: Sequence[float]) -> Distribution | None:
    if not values:
        return None
    return Distribution(
        p25=percentile(values, 0.25),
        median=percentile(values, 0.5),
        p75=percentile(values, 0.75),
        max=max(values),
    )


def _idiom_stats(censuses: Sequence[RepoCensus]) -> tuple[IdiomStat, ...]:
    analyzed = len(censuses)
    kinds = sorted({kind for census in censuses for kind in census.suspects})
    stats: list[IdiomStat] = []
    for kind in kinds:
        present = [census for census in censuses if census.suspects[kind].files > 0]
        reached = [census for census in present if census.suspects[kind].reached_files > 0]
        blasts = [
            blast for census in present if (blast := census.suspects[kind].blast_radius) is not None
        ]
        stats.append(
            IdiomStat(
                kind=kind,
                repos_present=len(present),
                repos_reached=len(reached),
                present_share=_share(len(present), analyzed),
                reached_share=_share(len(reached), analyzed),
                median_blast=percentile(blasts, 0.5) if blasts else None,
            )
        )
    return tuple(stats)


def _build_next(
    censuses: Sequence[RepoCensus], idioms: Sequence[IdiomStat]
) -> tuple[BuildItem, ...]:
    """Rank resolver candidates: frequency of pinned tests times median cost.

    Suspect kinds use the share of repos with test-reachable instances and the
    median per-kind blast radius over repos where the kind is present. Global
    rules (R006, R015) cost the whole suite wherever they fire. The fat-init
    entry counts repos whose worst top-level __init__.py pins at least half
    the suite, costed at the median worst-init exposure.
    """
    analyzed = len(censuses)
    items = [
        BuildItem(
            label=stat.kind,
            affected_repos=stat.repos_reached,
            affected_share=stat.reached_share,
            median_cost=stat.median_blast,
            score=stat.reached_share * (stat.median_blast or 0.0),
        )
        for stat in idioms
    ]
    r006 = sum(1 for census in censuses if census.r006_files)
    items.append(
        BuildItem(
            label=R006_LABEL,
            affected_repos=r006,
            affected_share=_share(r006, analyzed),
            median_cost=_GLOBAL_COST if r006 else None,
            score=_share(r006, analyzed) * _GLOBAL_COST,
        )
    )
    doctest = sum(1 for census in censuses if census.doctest_modules)
    items.append(
        BuildItem(
            label=R015_LABEL,
            affected_repos=doctest,
            affected_share=_share(doctest, analyzed),
            median_cost=_GLOBAL_COST if doctest else None,
            score=_share(doctest, analyzed) * _GLOBAL_COST,
        )
    )
    maxima = [census.fat_init_max for census in censuses if census.fat_init_max is not None]
    over = sum(1 for value in maxima if value >= FAT_INIT_THRESHOLD)
    median_max = percentile(maxima, 0.5) if maxima else None
    items.append(
        BuildItem(
            label=FAT_INIT_LABEL,
            affected_repos=over,
            affected_share=_share(over, analyzed),
            median_cost=median_max,
            score=_share(over, analyzed) * (median_max or 0.0),
        )
    )
    return tuple(sorted(items, key=lambda item: (-item.score, -item.affected_share, item.label)))


def summarize_census(
    censuses: Sequence[RepoCensus], failures: Sequence[RepoFailure]
) -> CensusSummary:
    """Fold per-repo censuses into the corpus summary; pure over its inputs."""
    idioms = _idiom_stats(censuses)
    taint = [blast for census in censuses if (blast := census.taint_blast_radius) is not None]
    maxima = [census.fat_init_max for census in censuses if census.fat_init_max is not None]
    unreached = [
        share for census in censuses if (share := census.unreached_hazard_share) is not None
    ]
    conftests = [float(census.conftests) for census in censuses]
    return CensusSummary(
        analyzed=len(censuses),
        failed=len(failures),
        zero_test_repos=sum(1 for census in censuses if census.test_files == 0),
        idioms=idioms,
        build_next=_build_next(censuses, idioms),
        taint_blast=_distribution(taint),
        fat_init=_distribution(maxima),
        fat_init_over_half=sum(1 for value in maxima if value >= FAT_INIT_THRESHOLD),
        unreached_share=_distribution(unreached),
        r006_repos=sum(1 for census in censuses if census.r006_files),
        doctest_repos=sum(1 for census in censuses if census.doctest_modules),
        conftest_median=percentile(conftests, 0.5) if conftests else None,
        failures=tuple(sorted(failures, key=lambda failure: failure.slug)),
    )


def _distribution_dict(distribution: Distribution | None) -> dict[str, float] | None:
    if distribution is None:
        return None
    return {
        "p25": round(distribution.p25, 4),
        "median": round(distribution.median, 4),
        "p75": round(distribution.p75, 4),
        "max": round(distribution.max, 4),
    }


def census_summary_to_dict(summary: CensusSummary) -> dict[str, Any]:
    return {
        "schema": CENSUS_SUMMARY_SCHEMA,
        "repos": {
            "analyzed": summary.analyzed,
            "failed": summary.failed,
            "zero_tests": summary.zero_test_repos,
        },
        "idioms": {
            stat.kind: {
                "repos_present": stat.repos_present,
                "present_share": round(stat.present_share, 4),
                "repos_reached": stat.repos_reached,
                "reached_share": round(stat.reached_share, 4),
                "median_blast_radius": _rounded(stat.median_blast),
            }
            for stat in summary.idioms
        },
        "build_next": [
            {
                "item": item.label,
                "affected_repos": item.affected_repos,
                "affected_share": round(item.affected_share, 4),
                "median_cost": _rounded(item.median_cost),
                "score": round(item.score, 4),
            }
            for item in summary.build_next
        ],
        "taint_blast_radius": _distribution_dict(summary.taint_blast),
        "fat_init": {
            "distribution": _distribution_dict(summary.fat_init),
            "repos_over_half": summary.fat_init_over_half,
        },
        "unreached_hazard_share": _distribution_dict(summary.unreached_share),
        "standing": {
            "r006_repos": summary.r006_repos,
            "doctest_repos": summary.doctest_repos,
            "conftest_count_median": (
                None if summary.conftest_median is None else round(summary.conftest_median, 1)
            ),
        },
        "failures": [
            {"slug": failure.slug, "stage": failure.stage, "error": failure.error}
            for failure in summary.failures
        ],
    }


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _cost_cell(value: float | None) -> str:
    return "-" if value is None else _pct(value)


def _distribution_row(label: str, distribution: Distribution | None) -> str:
    if distribution is None:
        return f"| {label} | - | - | - | - |"
    return (
        f"| {label} | {_pct(distribution.p25)} | {_pct(distribution.median)} "
        f"| {_pct(distribution.p75)} | {_pct(distribution.max)} |"
    )


def _table_text(raw: str, limit: int = 160) -> str:
    flattened = " ".join(raw.split()).replace("|", "\\|")
    return flattened if len(flattened) <= limit else flattened[: limit - 3] + "..."


def render_census_markdown(summary: CensusSummary) -> str:
    """Render the census summary page, deterministic for identical inputs."""
    median_taint = "-" if summary.taint_blast is None else _pct(summary.taint_blast.median)
    lines = [
        "# Acquit OSS idiom census",
        "",
        "Generated by `acquit-study census` from working-tree snapshots of",
        "shallow clones. Nothing in any surveyed repository is ever executed.",
        "Regenerate it after any run; never edit it by hand.",
        "",
        "## Headline",
        "",
        "| Repos analyzed | Failed | Zero-test repos | Median taint blast radius "
        "| R006 repos | R015 repos |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| {summary.analyzed} | {summary.failed} | {summary.zero_test_repos} "
        f"| {median_taint} | {summary.r006_repos} | {summary.doctest_repos} |",
        "",
        "## What to build next",
        "",
        "Score = (share of repos where the hazard pins at least one test) x",
        "(median share of the suite it pins where present). Global rules pin",
        "the whole suite wherever they fire. The fat-init row counts repos",
        "whose worst top-level `__init__.py` sits in at least half of all test",
        "closures, costed at the median worst-init exposure.",
        "",
        "| Rank | Hazard | Repos affected | Median blast radius | Score |",
        "| --- | --- | --- | --- | --- |",
    ]
    for rank, item in enumerate(summary.build_next, start=1):
        lines.append(
            f"| {rank} | {item.label} | {item.affected_repos} ({_pct(item.affected_share)}) "
            f"| {_cost_cell(item.median_cost)} | {item.score:.3f} |"
        )
    lines += [
        "",
        "## Idiom frequency",
        "",
        "| Idiom | Repos present | Repos with test-reachable instances "
        "| Median blast radius when present |",
        "| --- | --- | --- | --- |",
    ]
    for stat in summary.idioms:
        lines.append(
            f"| {stat.kind} | {stat.repos_present} ({_pct(stat.present_share)}) "
            f"| {stat.repos_reached} ({_pct(stat.reached_share)}) "
            f"| {_cost_cell(stat.median_blast)} |"
        )
    lines += [
        "",
        "## Distributions over analyzed repos",
        "",
        "| Metric | p25 | median | p75 | max |",
        "| --- | --- | --- | --- | --- |",
        _distribution_row(
            "taint blast radius (tests pinned by standing taints)", summary.taint_blast
        ),
        _distribution_row("fat-init exposure (worst top-level __init__.py)", summary.fat_init),
        _distribution_row(
            "unreached hazard share (suspect files no test reaches)", summary.unreached_share
        ),
        "",
        "## Standing hazards",
        "",
        f"- Repos with a collection-altering conftest (R006): {summary.r006_repos}",
        f"- Repos enabling --doctest-modules (R015): {summary.doctest_repos}",
        "- Median conftest.py count per repo: "
        + ("-" if summary.conftest_median is None else f"{summary.conftest_median:.1f}"),
        f"- Repos where one top-level __init__.py pins at least half the suite: "
        f"{summary.fat_init_over_half}",
        "",
        "## Failures",
        "",
        "| Repo | Stage | Error |",
        "| --- | --- | --- |",
    ]
    for failure in summary.failures:
        lines.append(f"| {failure.slug} | {failure.stage} | {_table_text(failure.error)} |")
    if not summary.failures:
        lines.append("| (none) | - | - |")
    return "\n".join(lines) + "\n"


def _ensure_clone(slug: str, clone_dir: Path) -> None:
    """Shallow-clone the repo, or reuse a clone left by an earlier run."""
    if (clone_dir / ".git").exists():
        return
    if clone_dir.exists():
        shutil.rmtree(clone_dir, ignore_errors=True)
    args = [
        "git",
        "-c",
        "core.longpaths=true",
        "clone",
        "--quiet",
        "--depth",
        "1",
        "--single-branch",
        f"https://github.com/{slug}.git",
        str(clone_dir),
    ]
    try:
        completed = subprocess.run(
            args,
            cwd=clone_dir.parent,
            capture_output=True,
            check=False,
            timeout=_CLONE_TIMEOUT_SECONDS,
        )
    except OSError as error:
        raise AcquitError(f"could not execute git: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise AcquitError(f"git clone timed out after {int(_CLONE_TIMEOUT_SECONDS)}s") from error
    if completed.returncode != 0:
        tail = completed.stderr.decode("utf-8", errors="replace").strip()[-500:]
        # A partial checkout must not satisfy the reuse check on the next run.
        shutil.rmtree(clone_dir, ignore_errors=True)
        raise AcquitError(f"git clone exited with {completed.returncode}: {tail}")


def _census_one(slug: str, workdir: Path) -> RepoCensus | RepoFailure:
    clone_dir = workdir / slug.replace("/", "__")
    try:
        _ensure_clone(slug, clone_dir)
    except AcquitError as error:
        return RepoFailure(slug=slug, stage="clone", error=str(error))
    try:
        pytest_config = load_pytest_config(clone_dir)
        snapshot = snapshot_tree(None, clone_dir, load_config(clone_dir), pytest_config, None)
    except (AcquitError, OSError, RecursionError) as error:
        # The census tolerates weird repos; the failure is data, not a crash.
        return RepoFailure(slug=slug, stage="snapshot", error=str(error))
    return census_of_snapshot(slug, snapshot, pytest_config)


def run_census(repos_path: Path, workdir: Path, out_dir: Path) -> int:
    """Survey every listed repo and write per-repo JSONs plus the summary."""
    try:
        text = repos_path.read_text(encoding="utf-8")
    except OSError as error:
        raise AcquitError(f"could not read repos list {repos_path}: {error}") from error
    slugs = parse_repos_list(text)
    if not slugs:
        raise AcquitError(f"no repositories listed in {repos_path}")
    workdir.mkdir(parents=True, exist_ok=True)
    results_dir = out_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    censuses: list[RepoCensus] = []
    failures: list[RepoFailure] = []
    for position, slug in enumerate(slugs, start=1):
        prefix = f"census: [{position}/{len(slugs)}] {slug}"
        outcome = _census_one(slug, workdir)
        if isinstance(outcome, RepoFailure):
            failures.append(outcome)
            payload = failure_to_dict(outcome)
            print(f"{prefix}: FAILED at {outcome.stage}: {outcome.error}", file=sys.stderr)
        else:
            censuses.append(outcome)
            payload = repo_census_to_dict(outcome)
            radius = outcome.taint_blast_radius
            blast = "-" if radius is None else f"{radius:.2f}"
            print(
                f"{prefix}: tests={outcome.test_files} modules={outcome.modules} "
                f"taint_blast={blast}"
            )
        out_path = results_dir / (slug.replace("/", "__") + ".json")
        out_path.write_text(to_canonical_json(payload), encoding="utf-8")

    summary = summarize_census(censuses, failures)
    summary_json = out_dir / "census-summary.json"
    summary_md = out_dir / "census-summary.md"
    summary_json.write_text(to_canonical_json(census_summary_to_dict(summary)), encoding="utf-8")
    summary_md.write_text(render_census_markdown(summary), encoding="utf-8")
    elapsed = time.monotonic() - started
    print(
        f"census: analyzed {len(censuses)} repo(s), {len(failures)} failure(s) "
        f"in {elapsed:.0f}s; wrote {summary_md} and {summary_json}"
    )
    return 0 if censuses else 1
