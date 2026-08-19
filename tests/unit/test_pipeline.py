"""End-to-end pipeline tests over throwaway git repositories."""

import shutil
from pathlib import Path

import pytest
from conftest import RepoBuilder, ScenarioRepo, module_test_source

from acquit import pipeline, vcs
from acquit.config import AcquitConfig, load_config
from acquit.graph.cache import ParseCache
from acquit.pipeline import Snapshot, run_select, snapshot_tree
from acquit.policy.model import RuleId
from acquit.pytestmap.pytestcfg import PytestConfig, load_pytest_config
from acquit.report import SelectionMode
from acquit.select import Decision, import_closure
from acquit.witness import CLAIM_NARROWED, ReliedInit, verify_witness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")


def selected_paths(decision: Decision) -> set[str]:
    return {entry.path for entry in decision.selected}


def skipped_paths(decision: Decision) -> set[str]:
    return {entry.path for entry in decision.skipped}


def reasons_of(decision: Decision, path: str) -> tuple[str, ...]:
    return next(entry.reasons for entry in decision.selected if entry.path == path)


def test_select_change_selects_only_the_importing_test(scenario_repo: ScenarioRepo) -> None:
    result = run_select(scenario_repo.base, scenario_repo.alpha_change, scenario_repo.path)

    assert result.decision.mode is SelectionMode.SELECTIVE
    assert selected_paths(result.decision) == {"tests/test_alpha.py"}
    assert reasons_of(result.decision, "tests/test_alpha.py") == ("reachable-from:alpha.py",)
    assert skipped_paths(result.decision) == {
        "tests/pkg/test_pkg.py",
        "tests/test_beta.py",
        "tests/test_delta.py",
    }
    assert result.base_sha == scenario_repo.base
    assert result.head_sha == scenario_repo.alpha_change


@pytest.mark.parametrize("invocation_subdir", ["", "backend"])
def test_nested_src_layout_never_skips_direct_importer(
    repo_builder: RepoBuilder, invocation_subdir: str
) -> None:
    repo_builder.write(
        {
            "backend/pyproject.toml": """\
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
""",
            "backend/src/app/__init__.py": "",
            "backend/src/app/service.py": "def answer() -> int:\n    return 41\n",
            "backend/tests/test_service.py": (
                "from app.service import answer\n\n\n"
                "def test_answer() -> None:\n"
                "    assert answer() == 41\n"
            ),
        }
    )
    base = repo_builder.commit("base")
    repo_builder.write({"backend/src/app/service.py": "def answer() -> int:\n    return 42\n"})
    head = repo_builder.commit("break service test")
    cwd = repo_builder.path / invocation_subdir

    result = run_select(base, head, cwd)

    assert result.head.index.roots == ("", "backend/src", "backend/tests")
    assert import_closure(result.head.graph, "backend/tests/test_service.py") >= {
        "backend/src/app/__init__.py",
        "backend/src/app/service.py",
    }
    assert result.decision.mode is SelectionMode.RUN_ALL
    assert result.decision.skipped == ()
    assert result.decision.witnesses == ()


def test_select_witnesses_verify_against_recomputed_closures(
    scenario_repo: ScenarioRepo,
) -> None:
    result = run_select(scenario_repo.base, scenario_repo.alpha_change, scenario_repo.path)

    by_id = {witness.id: witness for witness in result.decision.witnesses}
    assert len(by_id) == len(result.decision.skipped)
    for entry in result.decision.skipped:
        closure = import_closure(result.head.graph, entry.path)
        assert verify_witness(by_id[entry.witness_id], closure, {"alpha.py"})


def test_select_manifest_change_is_global_run_all(scenario_repo: ScenarioRepo) -> None:
    result = run_select(
        scenario_repo.conftest_change, scenario_repo.manifest_change, scenario_repo.path
    )

    assert RuleId.CHANGED_DEPENDENCY_MANIFEST in {f.rule for f in result.outcome.findings}
    assert result.decision.mode is SelectionMode.RUN_ALL
    assert result.decision.selected == ()
    assert result.decision.skipped == ()
    assert result.decision.witnesses == ()


def test_select_conftest_change_captures_its_subtree(scenario_repo: ScenarioRepo) -> None:
    result = run_select(
        scenario_repo.delta_removal, scenario_repo.conftest_change, scenario_repo.path
    )

    assert "rule:R005" in reasons_of(result.decision, "tests/pkg/test_pkg.py")
    assert skipped_paths(result.decision) == {
        "tests/test_alpha.py",
        "tests/test_beta.py",
        "tests/test_delta.py",
    }


def test_select_deletion_selects_importer_through_base_graph(
    scenario_repo: ScenarioRepo,
) -> None:
    result = run_select(scenario_repo.alpha_change, scenario_repo.delta_removal, scenario_repo.path)

    # delta_extra.py exists only at base, so this reason proves the base graph ran.
    assert "reachable-from:delta_extra.py" in reasons_of(result.decision, "tests/test_delta.py")
    assert skipped_paths(result.decision) == {
        "tests/pkg/test_pkg.py",
        "tests/test_alpha.py",
        "tests/test_beta.py",
    }
    for witness in result.decision.witnesses:
        assert witness.changed == ("delta.py", "delta_extra.py")


def test_escalated_mutator_skips_the_base_snapshot(
    repo_builder: RepoBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_builder.write(
        {
            "paths.py": "import sys\n\nsys.path.append('vendor')\n",
            "doomed.py": "D = 1\n",
            "tests/test_paths.py": module_test_source("paths"),
        }
    )
    base = repo_builder.commit("base")
    repo_builder.remove("doomed.py")
    head = repo_builder.commit("delete doomed")

    snapshotted: list[str | None] = []

    def counting(
        ref: str | None,
        repo: Path,
        acquit_config: AcquitConfig,
        pytest_config: PytestConfig,
        cache: ParseCache | None,
    ) -> Snapshot:
        snapshotted.append(ref)
        return snapshot_tree(ref, repo, acquit_config, pytest_config, cache)

    monkeypatch.setattr(pipeline, "snapshot_tree", counting)
    result = run_select(base, head, repo_builder.path)

    # The deletion alone would demand a base snapshot; the escalated mutator
    # already binds the run to run-all, so only head is analyzed.
    assert result.decision.mode is SelectionMode.RUN_ALL
    assert snapshotted == [head]


def test_select_working_tree_head_has_no_head_sha(repo_builder: RepoBuilder) -> None:
    repo_builder.write(
        {
            "mod.py": "X = 1\n",
            "other.py": "Y = 1\n",
            "tests/test_mod.py": module_test_source("mod"),
            "tests/test_other.py": module_test_source("other"),
        }
    )
    base = repo_builder.commit("base")
    repo_builder.write({"mod.py": "X = 2\n"})

    result = run_select(base, None, repo_builder.path)

    assert result.head_sha is None
    assert result.base_sha == base
    assert selected_paths(result.decision) == {"tests/test_mod.py"}
    assert skipped_paths(result.decision) == {"tests/test_other.py"}


def test_untracked_files_are_added_changes_in_working_tree_mode(
    repo_builder: RepoBuilder,
) -> None:
    repo_builder.write(
        {
            "mod.py": "X = 1\n",
            "tests/test_mod.py": module_test_source("mod"),
        }
    )
    base = repo_builder.commit("base")
    repo_builder.write(
        {
            "helper.py": "H = 1\n",
            "tests/test_new.py": module_test_source("helper"),
        }
    )

    result = run_select(base, None, repo_builder.path)

    statuses = {change.path: change.status for change in result.changed}
    assert statuses == {
        "helper.py": vcs.ChangeStatus.ADDED,
        "tests/test_new.py": vcs.ChangeStatus.ADDED,
    }
    assert selected_paths(result.decision) == {"tests/test_new.py"}
    assert skipped_paths(result.decision) == {"tests/test_mod.py"}


def test_snapshot_of_clean_working_tree_matches_head_snapshot(
    scenario_repo: ScenarioRepo,
) -> None:
    repo = scenario_repo.path
    acquit_config = load_config(repo)
    pytest_config = load_pytest_config(repo)

    working = snapshot_tree(None, repo, acquit_config, pytest_config, None)
    at_head = snapshot_tree("HEAD", repo, acquit_config, pytest_config, None)

    assert working.graph.graph_hash == at_head.graph.graph_hash
    assert working.files == at_head.files


def test_clean_working_tree_fingerprint_matches_head_fingerprint(
    scenario_repo: ScenarioRepo,
) -> None:
    repo = scenario_repo.path
    assert vcs.working_tree_fingerprint(repo) == vcs.ref_tree_fingerprint("HEAD", repo)


def test_dirty_file_changes_the_working_tree_fingerprint(repo_builder: RepoBuilder) -> None:
    repo_builder.write({"mod.py": "X = 1\n", "res.txt": "data\n"})
    repo_builder.commit("base")
    clean = vcs.working_tree_fingerprint(repo_builder.path)

    # Resources count too: the fingerprint binds content, not just imports.
    repo_builder.write({"res.txt": "changed\n"})

    assert vcs.working_tree_fingerprint(repo_builder.path) != clean


def test_excluded_paths_leave_the_working_tree_fingerprint_alone(
    repo_builder: RepoBuilder,
) -> None:
    repo_builder.write({"mod.py": "X = 1\n"})
    repo_builder.commit("base")
    clean = vcs.working_tree_fingerprint(repo_builder.path)

    repo_builder.write({"acquit-report.json": "{}\n"})

    assert vcs.working_tree_fingerprint(repo_builder.path) != clean
    excluded = frozenset({"acquit-report.json"})
    assert vcs.working_tree_fingerprint(repo_builder.path, excluded) == clean


def test_working_tree_blob_shas_match_git(scenario_repo: ScenarioRepo, tmp_path: Path) -> None:
    repo = scenario_repo.path
    cache_root = tmp_path / "parse"

    snapshot_tree(None, repo, load_config(repo), load_pytest_config(repo), ParseCache(cache_root))

    git_sha = vcs.blob_shas("HEAD", repo)["alpha.py"]
    assert (cache_root / f"{git_sha}.json").is_file()


def test_identical_files_keep_their_own_paths_through_the_cache(
    repo_builder: RepoBuilder, tmp_path: Path
) -> None:
    repo_builder.write(
        {
            "shared.py": "S = 1\n",
            "a.py": "import shared\n",
            "b.py": "import shared\n",
        }
    )
    repo_builder.commit("base")
    repo = repo_builder.path

    snapshot = snapshot_tree(
        "HEAD",
        repo,
        load_config(repo),
        load_pytest_config(repo),
        ParseCache(tmp_path / "cache-probe"),
    )

    assert snapshot.facts["a.py"].path == "a.py"
    assert snapshot.facts["b.py"].path == "b.py"
    graph = snapshot.graph
    assert graph.digraph.has_edge(graph.index_of["a.py"], graph.index_of["shared.py"])
    assert graph.digraph.has_edge(graph.index_of["b.py"], graph.index_of["shared.py"])


PKG_INIT = (
    '"""Pure re-exporter."""\n\nfrom .console import Console\nfrom .table import Table\n\n'
    '__all__ = ["Console", "Table"]\n'
)
PKG_TABLE = (
    '"""Inert sibling."""\n\n\nclass Table:\n    def render(self) -> str:\n        return "table"\n'
)
PKG_TABLE_EDIT = (
    '"""Inert sibling."""\n\n\nclass Table:\n    def render(self) -> str:\n        return "grid"\n'
)
PKG_CONSOLE = (
    '"""Not inert."""\n\nSTATE = dict(fancy="*")\n\n\nclass Console:\n'
    '    def banner(self) -> str:\n        return "console"\n'
)


def write_reexport_repo(builder: RepoBuilder, *, narrowing: bool) -> str:
    files = {
        "pkg/__init__.py": PKG_INIT,
        "pkg/table.py": PKG_TABLE,
        "pkg/console.py": PKG_CONSOLE,
        "free.py": "FREE = 1\n",
        "test_console.py": "from pkg import Console\n\n\ndef test_console():\n    assert Console\n",
        "test_table.py": "from pkg import Table\n\n\ndef test_table():\n    assert Table\n",
        "test_free.py": module_test_source("free"),
    }
    if narrowing:
        files[".acquit.toml"] = "narrowing = true\n"
    builder.write(files)
    return builder.commit("base")


def test_narrowing_enabled_skips_the_other_symbol_consumer(repo_builder: RepoBuilder) -> None:
    base = write_reexport_repo(repo_builder, narrowing=True)
    repo_builder.write({"pkg/table.py": PKG_TABLE_EDIT})
    head = repo_builder.commit("edit table body")

    result = run_select(base, head, repo_builder.path)

    assert result.decision.mode is SelectionMode.SELECTIVE
    assert selected_paths(result.decision) == {"test_table.py"}
    assert reasons_of(result.decision, "test_table.py") == (
        "narrowing-refused:inside-semantic-closure",
        "reachable-from:pkg/table.py",
    )
    skipped = {entry.path: entry for entry in result.decision.skipped}
    assert set(skipped) == {"test_console.py", "test_free.py"}
    assert skipped["test_console.py"].narrowed
    assert not skipped["test_free.py"].narrowed

    witness = next(w for w in result.decision.witnesses if w.test == "test_console.py")
    assert witness.claim == CLAIM_NARROWED
    (block,) = witness.narrowed
    assert block.path == "pkg/table.py"
    assert block.base_blob == vcs.blob_shas(base, repo_builder.path)["pkg/table.py"]
    assert block.head_blob == vcs.blob_shas(head, repo_builder.path)["pkg/table.py"]
    assert block.inits == (
        ReliedInit(path="pkg/__init__.py", base_tier="strict", head_tier="strict"),
    )
    closure = import_closure(result.head.graph, "test_console.py")
    assert verify_witness(witness, closure, {"pkg/table.py"})


def test_narrowing_disabled_selects_both_consumers(repo_builder: RepoBuilder) -> None:
    base = write_reexport_repo(repo_builder, narrowing=False)
    repo_builder.write({"pkg/table.py": PKG_TABLE_EDIT})
    head = repo_builder.commit("edit table body")

    result = run_select(base, head, repo_builder.path)

    assert selected_paths(result.decision) == {"test_console.py", "test_table.py"}
    for entry in result.decision.selected:
        assert all(not reason.startswith("narrowing") for reason in entry.reasons)
    assert skipped_paths(result.decision) == {"test_free.py"}
    assert all(not entry.narrowed for entry in result.decision.skipped)
    assert all(not witness.narrowed for witness in result.decision.witnesses)


def test_narrowing_never_engages_on_the_working_tree(repo_builder: RepoBuilder) -> None:
    base = write_reexport_repo(repo_builder, narrowing=True)
    repo_builder.write({"pkg/table.py": PKG_TABLE_EDIT})

    result = run_select(base, None, repo_builder.path)

    assert result.head_sha is None
    assert selected_paths(result.decision) == {"test_console.py", "test_table.py"}
    for entry in result.decision.selected:
        assert all(not reason.startswith("narrowing") for reason in entry.reasons)
    assert all(not entry.narrowed for entry in result.decision.skipped)


def test_unparseable_file_taints_and_forces_its_reachers(repo_builder: RepoBuilder) -> None:
    repo_builder.write(
        {
            "bad.py": "def broken(:\n",
            "good.py": "G = 1\n",
            "tests/test_bad.py": "import bad\n\n\ndef test_bad():\n    assert True\n",
            "tests/test_good.py": module_test_source("good"),
        }
    )
    base = repo_builder.commit("base")
    repo_builder.write({"good.py": "G = 2\n"})
    head = repo_builder.commit("head")

    result = run_select(base, head, repo_builder.path)

    assert "bad.py" in result.head.unparseable
    assert result.head.graph.nodes["bad.py"].tainted
    assert RuleId.UNPARSEABLE_FILE in {f.rule for f in result.outcome.findings}
    always = {entry.path: entry.finding for entry in result.decision.always_run}
    assert always == {"tests/test_bad.py": "R010:bad.py"}
    assert selected_paths(result.decision) == {"tests/test_good.py"}
