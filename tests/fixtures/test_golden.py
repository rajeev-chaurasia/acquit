"""Golden tests: every fixture repo's graph and selection scenarios, end to end.

The graph assertion doubles as a cross-platform determinism check: the JSON
goldens were generated once and must be reproduced byte-for-byte from a fresh
snapshot on any machine. On mismatch the full actual document is printed so a
deliberate regeneration is a copy-paste away.
"""

import json
import shutil
from collections.abc import Callable
from typing import Any

import pytest

from acquit.graph.model import NodeKind
from acquit.pipeline import SelectResult, run_select
from acquit.report import SelectionMode
from acquit.select import import_closure
from acquit.witness import verify_witness
from fixtures.conftest import (
    FIXTURE_NAMES,
    FixtureRepo,
    apply_scenario_change,
    commit_all,
    full_changed_paths,
    graph_as_dict,
    load_expected_graph,
    load_scenarios,
    run_git,
    snapshot_working_tree_plain,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")

SCENARIOS = [
    pytest.param(name, scenario, id=f"{name}-{scenario['name']}")
    for name in FIXTURE_NAMES
    for scenario in load_scenarios(name)
]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_graph_matches_golden(name: str, repo_cache: Callable[[str], FixtureRepo]) -> None:
    built = repo_cache(name)
    snapshot = snapshot_working_tree_plain(built.path)
    actual = graph_as_dict(snapshot.graph)
    expected = load_expected_graph(name)
    if actual != expected:
        print(f"=== actual graph for fixture {name!r} ===")
        print(json.dumps(actual, indent=2, sort_keys=True))
        pytest.fail(f"graph for {name} diverges from expected_graph.json; actual printed above")


def _run_scenario(built: FixtureRepo, scenario: dict[str, Any]) -> SelectResult:
    apply_scenario_change(built.path, scenario["changed"])
    try:
        head_sha = commit_all(built.path, f"scenario {scenario['name']}")
        return run_select(built.base_sha, head_sha, built.path)
    finally:
        run_git(built.path, "reset", "-q", "--hard", built.base_sha)


def _assert_witnesses_verify(result: SelectResult) -> None:
    changed = full_changed_paths(result.changed)
    by_id = {witness.id: witness for witness in result.decision.witnesses}
    assert len(by_id) == len(result.decision.skipped)
    for entry in result.decision.skipped:
        closure = import_closure(result.head.graph, entry.path)
        assert verify_witness(by_id[entry.witness_id], closure, changed), entry.path


@pytest.mark.parametrize(("name", "scenario"), SCENARIOS)
def test_scenario(
    name: str, scenario: dict[str, Any], repo_cache: Callable[[str], FixtureRepo]
) -> None:
    built = repo_cache(name)
    result = _run_scenario(built, scenario)
    decision = result.decision

    selected = {entry.path for entry in decision.selected}
    skipped = {entry.path for entry in decision.skipped}
    always = {entry.path: entry.finding for entry in decision.always_run}
    head_tests = {
        path for path, node in result.head.graph.nodes.items() if node.kind is NodeKind.TEST
    }

    assert str(decision.mode) == scenario["expect_mode"]
    assert bool(skipped) == (decision.mode is SelectionMode.SELECTIVE)
    if selected or skipped or always:
        # A global short-circuit reports nothing per test; anything else
        # must partition the head test set exactly.
        assert selected | skipped | set(always) == head_tests
        assert not selected & skipped
        assert not selected & set(always)
        assert not skipped & set(always)

    if "expect_rules" in scenario:
        fired = sorted({str(finding.rule) for finding in result.outcome.findings})
        assert fired == sorted(scenario["expect_rules"])
    if "expect_skipped" in scenario:
        assert skipped == set(scenario["expect_skipped"])
    if "expect_selected_contains" in scenario:
        assert set(scenario["expect_selected_contains"]) <= selected
    if "expect_always_run" in scenario:
        assert always == scenario["expect_always_run"]

    _assert_witnesses_verify(result)
