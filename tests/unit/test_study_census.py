"""Census computations over hand-built snapshots: no cloning, no network."""

from collections.abc import Mapping

import pytest

from acquit.errors import AcquitError, ParseFailure
from acquit.graph.build import assemble_graph
from acquit.graph.index import build_index, detect_roots, pytest_sys_path_roots
from acquit.graph.model import NodeKind
from acquit.graph.parse import ModuleFacts, parse_module_facts
from acquit.pipeline import Snapshot
from acquit.pytestmap.conftree import inspect_conftest
from acquit.pytestmap.discover import classify_file, discover_test_files
from acquit.pytestmap.pytestcfg import (
    DEFAULT_NORECURSEDIRS,
    DEFAULT_PYTHON_FILES,
    PytestConfig,
)
from acquit.study.census import (
    FAT_INIT_LABEL,
    R006_LABEL,
    R015_LABEL,
    KindCensus,
    RepoCensus,
    RepoFailure,
    census_of_snapshot,
    census_summary_to_dict,
    parse_repos_list,
    render_census_markdown,
    repo_census_to_dict,
    summarize_census,
)


def _pytest_config(doctest_modules: bool = False, source: str | None = None) -> PytestConfig:
    return PytestConfig(
        source=source,
        python_files=DEFAULT_PYTHON_FILES,
        testpaths=(),
        norecursedirs=DEFAULT_NORECURSEDIRS,
        addopts=("--doctest-modules",) if doctest_modules else (),
        pythonpath=(),
        doctest_modules=doctest_modules,
        extra_plugins=(),
    )


def _snapshot(sources: Mapping[str, str], pytest_config: PytestConfig) -> Snapshot:
    """Assemble a Snapshot from in-memory sources, mirroring snapshot_tree."""
    files = tuple(sorted(sources))
    py_files = tuple(path for path in files if path.endswith(".py"))
    facts: dict[str, ModuleFacts] = {}
    unparseable: list[str] = []
    for path in py_files:
        try:
            facts[path] = parse_module_facts(sources[path].encode("utf-8"), path)
        except ParseFailure:
            unparseable.append(path)
    tests = frozenset(discover_test_files(files, pytest_config))
    kinds = {path: classify_file(path, pytest_config, tests) for path in files}
    roots = detect_roots(files, None)
    runtime_roots = pytest_sys_path_roots(files, tests, pytest_config.pythonpath)
    index = build_index(py_files, (*roots, *runtime_roots))
    conftest_facts = {
        path: inspect_conftest(sources[path].encode("utf-8"), path)
        for path in files
        if kinds[path] is NodeKind.CONFTEST
    }
    graph = assemble_graph(
        files, kinds, facts, tuple(sorted(unparseable)), index, conftest_facts, pytest_config
    )
    return Snapshot(
        ref=None,
        files=files,
        kinds=kinds,
        facts=facts,
        unparseable=tuple(sorted(unparseable)),
        index=index,
        conftest_facts=conftest_facts,
        graph=graph,
    )


_FIXTURE_SOURCES = {
    "pkg/__init__.py": "",
    "pkg/dynamic.py": (
        "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n"
    ),
    "pkg/clean.py": "VALUE = 1\n",
    "pkg/evil.py": "exec('x = 1')\neval('x')\n",
    "orphan.py": "exec('y = 2')\n",
    "conftest.py": "def pytest_ignore_collect(collection_path):\n    return False\n",
    "tests/test_dynamic.py": (
        "import pkg.dynamic\n\n\ndef test_dynamic():\n    assert pkg.dynamic\n"
    ),
    "tests/test_clean.py": "import pkg.clean\n\n\ndef test_clean():\n    assert pkg.clean\n",
}


@pytest.fixture(name="fixture_census")
def fixture_census_fixture() -> RepoCensus:
    config = _pytest_config()
    return census_of_snapshot("acme/widget", _snapshot(_FIXTURE_SOURCES, config), config)


def test_counts_cover_files_tests_and_graph(fixture_census: RepoCensus) -> None:
    assert fixture_census.slug == "acme/widget"
    assert fixture_census.files == 8
    assert fixture_census.python_files == 8
    assert fixture_census.test_files == 2
    assert fixture_census.conftests == 1
    assert fixture_census.modules == 5
    assert fixture_census.unparseable == 0
    assert fixture_census.graph_nodes >= 8
    assert fixture_census.graph_edges > 0


def test_suspect_counting_files_occurrences_and_offenders(fixture_census: RepoCensus) -> None:
    exec_eval = fixture_census.suspects["exec-eval"]
    assert exec_eval.files == 2
    assert exec_eval.occurrences == 3
    assert exec_eval.top_offenders == (("pkg/evil.py", 2), ("orphan.py", 1))

    dynamic = fixture_census.suspects["non-literal-dynamic-import"]
    assert dynamic.files == 1
    assert dynamic.occurrences == 1
    assert dynamic.top_offenders == (("pkg/dynamic.py", 1),)


def test_reachability_splits_reached_from_unreached_carriers(
    fixture_census: RepoCensus,
) -> None:
    assert fixture_census.suspects["non-literal-dynamic-import"].reached_files == 1
    assert fixture_census.suspects["exec-eval"].reached_files == 0
    assert fixture_census.suspects["non-literal-dynamic-import"].blast_radius == 0.5
    assert fixture_census.suspects["exec-eval"].blast_radius == 0.0


def test_taint_blast_radius_is_share_of_tests_pinned(fixture_census: RepoCensus) -> None:
    assert fixture_census.tainted_reachers == 1
    assert fixture_census.taint_blast_radius == 0.5


def test_unreached_hazard_share_counts_carriers_no_test_reaches(
    fixture_census: RepoCensus,
) -> None:
    assert fixture_census.suspect_files == 3
    assert fixture_census.unreached_suspect_files == 2
    assert fixture_census.unreached_hazard_share == pytest.approx(2 / 3)


def test_fat_init_share_and_max(fixture_census: RepoCensus) -> None:
    # Both tests import through pkg/__init__.py, so it pins the whole suite.
    assert ("pkg/__init__.py", 1.0) in fixture_census.fat_inits
    assert fixture_census.fat_init_max == 1.0


def test_standing_hazards_r006_and_r015(fixture_census: RepoCensus) -> None:
    assert fixture_census.r006_files == ("conftest.py",)
    assert fixture_census.doctest_modules is False
    assert fixture_census.doctest_source is None

    config = _pytest_config(doctest_modules=True, source="pyproject.toml")
    census = census_of_snapshot("acme/docs", _snapshot(_FIXTURE_SOURCES, config), config)
    assert census.doctest_modules is True
    assert census.doctest_source == "pyproject.toml"


def test_zero_test_repo_reports_none_for_ratios() -> None:
    sources = {"solo.py": "exec('z')\n"}
    config = _pytest_config()
    census = census_of_snapshot("acme/empty", _snapshot(sources, config), config)
    assert census.test_files == 0
    assert census.taint_blast_radius is None
    assert census.suspects["exec-eval"].blast_radius is None
    assert census.fat_inits == ()
    assert census.fat_init_max is None
    assert census.unreached_hazard_share == 1.0


def test_top_offenders_cap_at_five() -> None:
    sources = {f"mod_{index}.py": "exec('a')\n" for index in range(7)}
    config = _pytest_config()
    census = census_of_snapshot("acme/many", _snapshot(sources, config), config)
    offenders = census.suspects["exec-eval"].top_offenders
    assert len(offenders) == 5
    assert offenders[0] == ("mod_0.py", 1)


def test_unparseable_files_are_counted() -> None:
    sources = {"broken.py": "def broken(:\n", "tests/test_a.py": "def test_a():\n    pass\n"}
    config = _pytest_config()
    census = census_of_snapshot("acme/broken", _snapshot(sources, config), config)
    assert census.unparseable == 1
    # Unparseable files taint their node without carrying parsed suspects.
    assert census.suspect_files == 0


def test_repo_census_dict_is_schema_tagged_and_sorted(fixture_census: RepoCensus) -> None:
    payload = repo_census_to_dict(fixture_census)
    assert payload["schema"] == "acquit/census-repo-v1"
    assert list(payload["suspects"]) == sorted(payload["suspects"])
    exec_eval = payload["suspects"]["exec-eval"]
    assert exec_eval["top_offenders"][0] == {"path": "pkg/evil.py", "suspects": 2}
    assert payload["unreached"]["share"] == round(2 / 3, 4)


def _census(slug: str, **overrides: object) -> RepoCensus:
    base = {
        "slug": slug,
        "files": 10,
        "python_files": 8,
        "modules": 5,
        "test_files": 4,
        "conftests": 1,
        "unparseable": 0,
        "graph_nodes": 10,
        "graph_edges": 12,
        "suspects": {},
        "tainted_reachers": 2,
        "taint_blast_radius": 0.5,
        "r006_files": (),
        "doctest_modules": False,
        "doctest_source": None,
        "fat_inits": (),
        "fat_init_max": None,
        "suspect_files": 0,
        "unreached_suspect_files": 0,
        "unreached_hazard_share": None,
    }
    base.update(overrides)
    return RepoCensus(**base)  # type: ignore[arg-type]


def _kind(files: int, reached: int, blast: float | None) -> dict[str, object]:
    return {
        "exec-eval": KindCensus(
            files=files,
            occurrences=files,
            reached_files=reached,
            blast_radius=blast,
            top_offenders=(),
        )
    }


def test_aggregation_ranks_by_frequency_times_blast() -> None:
    censuses = (
        _census("a/a", suspects=_kind(3, 2, 0.8), r006_files=("conftest.py",)),
        _census("b/b", suspects=_kind(1, 1, 0.4)),
        _census("c/c", suspects=_kind(0, 0, 0.0), doctest_modules=True),
        _census(
            "d/d",
            suspects=_kind(0, 0, 0.0),
            fat_init_max=0.9,
            fat_inits=(("p/__init__.py", 0.9),),
        ),
    )
    summary = summarize_census(censuses, ())

    assert summary.analyzed == 4
    (stat,) = summary.idioms
    assert stat.kind == "exec-eval"
    assert stat.repos_present == 2
    assert stat.repos_reached == 2
    assert stat.median_blast == pytest.approx(0.6)

    by_label = {item.label: item for item in summary.build_next}
    assert by_label["exec-eval"].score == pytest.approx(0.5 * 0.6)
    assert by_label[R006_LABEL].score == pytest.approx(0.25)
    assert by_label[R015_LABEL].score == pytest.approx(0.25)
    assert by_label[FAT_INIT_LABEL].score == pytest.approx(0.25 * 0.9)
    assert summary.build_next[0].label == "exec-eval"

    scores = [item.score for item in summary.build_next]
    assert scores == sorted(scores, reverse=True)


def test_aggregation_distributions_and_standing_counts() -> None:
    censuses = (
        _census("a/a", taint_blast_radius=0.2, conftests=1),
        _census("b/b", taint_blast_radius=0.8, conftests=3),
        _census("c/c", taint_blast_radius=None, test_files=0, conftests=2),
    )
    failures = (RepoFailure(slug="z/z", stage="clone", error="boom"),)
    summary = summarize_census(censuses, failures)

    assert summary.analyzed == 3
    assert summary.failed == 1
    assert summary.zero_test_repos == 1
    assert summary.taint_blast is not None
    assert summary.taint_blast.median == pytest.approx(0.5)
    assert summary.conftest_median == 2.0
    assert summary.failures[0].slug == "z/z"


def test_summary_dict_carries_schema_and_ranked_items() -> None:
    summary = summarize_census((_census("a/a", suspects=_kind(1, 1, 1.0)),), ())
    payload = census_summary_to_dict(summary)
    assert payload["schema"] == "acquit/census-summary-v1"
    assert payload["repos"] == {"analyzed": 1, "failed": 0, "zero_tests": 0}
    assert payload["build_next"][0]["item"] == "exec-eval"
    assert payload["build_next"][0]["score"] == 1.0


def test_markdown_rendering_is_deterministic() -> None:
    censuses = (
        _census("a/a", suspects=_kind(2, 1, 0.5), r006_files=("conftest.py",)),
        _census("b/b", suspects=_kind(1, 0, 0.0), unreached_hazard_share=1.0, suspect_files=1),
    )
    failures = (RepoFailure(slug="z/z", stage="snapshot", error="pipe | and\nnewline"),)
    summary = summarize_census(censuses, failures)

    first = render_census_markdown(summary)
    second = render_census_markdown(summarize_census(censuses, failures))
    assert first == second
    assert "# Acquit OSS idiom census" in first
    assert "## What to build next" in first
    assert "| 1 | " in first
    # Table cells never leak raw pipes or newlines from error text.
    assert "pipe \\| and newline" in first
    assert first.endswith("\n")


def test_markdown_handles_empty_corpus() -> None:
    text = render_census_markdown(summarize_census((), ()))
    assert "| 0 | 0 | 0 | - | 0 | 0 |" in text
    assert "| (none) | - | - |" in text


def test_parse_repos_list_comments_blanks_dedupe() -> None:
    text = "# corpus\npallets/flask\n\npallets/click # cli\npallets/flask\n"
    assert parse_repos_list(text) == ("pallets/flask", "pallets/click")


def test_parse_repos_list_rejects_bad_slug() -> None:
    with pytest.raises(AcquitError, match="line 2"):
        parse_repos_list("pallets/flask\nnot a slug\n")
