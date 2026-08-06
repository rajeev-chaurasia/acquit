"""End-to-end pipeline tests over throwaway git repositories."""

import shutil

import pytest
from conftest import RepoBuilder, ScenarioRepo, module_test_source

from acquit import vcs
from acquit.config import load_config
from acquit.constants import PARSE_CACHE_DIR
from acquit.graph.cache import ParseCache
from acquit.pipeline import run_select, snapshot_tree
from acquit.policy.model import RuleId
from acquit.pytestmap.pytestcfg import load_pytest_config
from acquit.report import SelectionMode
from acquit.select import Decision, import_closure
from acquit.witness import verify_witness

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


def test_working_tree_blob_shas_match_git(scenario_repo: ScenarioRepo) -> None:
    repo = scenario_repo.path
    cache_root = repo / PARSE_CACHE_DIR

    snapshot_tree(None, repo, load_config(repo), load_pytest_config(repo), ParseCache(cache_root))

    git_sha = vcs.blob_shas("HEAD", repo)["alpha.py"]
    assert (cache_root / f"{git_sha}.json").is_file()


def test_identical_files_keep_their_own_paths_through_the_cache(
    repo_builder: RepoBuilder,
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
        ParseCache(repo / PARSE_CACHE_DIR),
    )

    assert snapshot.facts["a.py"].path == "a.py"
    assert snapshot.facts["b.py"].path == "b.py"
    graph = snapshot.graph
    assert graph.digraph.has_edge(graph.index_of["a.py"], graph.index_of["shared.py"])
    assert graph.digraph.has_edge(graph.index_of["b.py"], graph.index_of["shared.py"])


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
