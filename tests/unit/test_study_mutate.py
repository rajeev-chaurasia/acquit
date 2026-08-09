"""The mutation-injection arm's pure pieces: enumeration determinism, the
ADR 0008 targeting filter, cap spacing, planning, parity math, and the
aggregate rendering of mutant data (kept here beside the arm they test)."""

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from acquit.study import RESULT_SCHEMA
from acquit.study.aggregate import load_results, run_aggregate, summarize
from acquit.study.mutate import (
    Mutant,
    MutantKind,
    MutantTarget,
    detection_parity,
    enumerate_mutants,
)
from acquit.study.runner import plan_mutants

KINDS_SOURCE = '''\
"""module docstring."""
FLAG = True
COUNT = 3
LABEL = "name"

def check(a, b):
    if a < b:
        return a == b
    return a + b
'''

RESTRICTED_SOURCE = '''\
"""doc."""
import os

LIMIT = 5
NAMES: list[str] = ["alpha"]

if os.sep == "/":
    FALLBACK = 1

def scale(value, factor=2):
    return value + LIMIT

class Config:
    retries = 7
'''


def test_enumeration_is_deterministic() -> None:
    first = enumerate_mutants(KINDS_SOURCE)
    second = enumerate_mutants(KINDS_SOURCE)
    assert first
    assert first == second


def test_enumeration_orders_by_position() -> None:
    mutants = enumerate_mutants(KINDS_SOURCE)
    positions = [(mutant.line, mutant.col) for mutant in mutants]
    assert positions == sorted(positions)


def test_expected_kinds_and_descriptions() -> None:
    mutants = enumerate_mutants(KINDS_SOURCE)
    by_description = {(mutant.kind, mutant.description) for mutant in mutants}
    assert by_description == {
        (MutantKind.BOOL_FLIP, "True to False"),
        (MutantKind.BOUNDARY, "3 to 4"),
        (MutantKind.STRING_TWEAK, "string constant extended"),
        (MutantKind.COMPARE_FLIP, "< to <="),
        (MutantKind.COMPARE_FLIP, "== to !="),
        (MutantKind.RETURN_NEGATE, "return value negated"),
        (MutantKind.ARITH_FLIP, "+ to -"),
    }


def test_mutants_compile_and_differ_from_original() -> None:
    pristine = ast.unparse(ast.parse(KINDS_SOURCE))
    for mutant in enumerate_mutants(KINDS_SOURCE):
        compile(mutant.source, "<mutant>", "exec")
        assert mutant.source != pristine


def test_docstrings_are_not_mutated() -> None:
    source = '"""module doc."""\n\n\ndef f():\n    """fn doc."""\n'
    assert enumerate_mutants(source) == []


def test_unparseable_source_yields_no_mutants() -> None:
    assert enumerate_mutants("def broken(:\n") == []


def test_cap_takes_evenly_spaced_slice() -> None:
    source = "VALUES = [" + ", ".join(str(index) for index in range(26)) + "]"
    full = enumerate_mutants(source, cap=100)
    assert len(full) == 26
    capped = enumerate_mutants(source, cap=5)
    assert capped == [full[0], full[6], full[12], full[18], full[25]]


def test_cap_zero_yields_no_mutants() -> None:
    assert enumerate_mutants(KINDS_SOURCE, cap=0) == []


def test_restricted_target_keeps_bodies_and_module_constants() -> None:
    mutants = enumerate_mutants(
        RESTRICTED_SOURCE, target=MutantTarget.FUNCTION_BODIES_AND_CONSTANTS
    )
    kinds = {(mutant.kind, mutant.description) for mutant in mutants}
    assert kinds == {
        (MutantKind.BOUNDARY, "5 to 6"),
        (MutantKind.STRING_TWEAK, "string constant extended"),
        (MutantKind.ARITH_FLIP, "+ to -"),
        (MutantKind.BOUNDARY, "7 to 8"),
    }


def test_restricted_target_excludes_module_level_statements() -> None:
    everything = enumerate_mutants(RESTRICTED_SOURCE, cap=100)
    restricted = enumerate_mutants(
        RESTRICTED_SOURCE, target=MutantTarget.FUNCTION_BODIES_AND_CONSTANTS, cap=100
    )
    dropped = {
        (mutant.kind, mutant.description) for mutant in everything if mutant not in restricted
    }
    # the if statement's compare and string, the assignment inside its body,
    # and the def's default value all execute at import time and are excluded
    assert dropped == {
        (MutantKind.COMPARE_FLIP, "== to !="),
        (MutantKind.STRING_TWEAK, "string constant extended"),
        (MutantKind.BOUNDARY, "1 to 2"),
        (MutantKind.BOUNDARY, "2 to 3"),
    }


def test_detection_parity_math() -> None:
    assert detection_parity([(True, True), (False, True)]) == pytest.approx(0.5)
    assert detection_parity([(True, True), (True, True)]) == 1.0
    assert detection_parity([(False, True)]) == 0.0


def test_detection_parity_is_one_when_full_kills_none() -> None:
    assert detection_parity([]) == 1.0
    assert detection_parity([(False, False), (True, False)]) == 1.0


def _mutant(tag: str) -> Mutant:
    return Mutant(source=tag, line=1, col=0, kind=MutantKind.BOUNDARY, description=tag)


def test_plan_mutants_round_robins_across_files() -> None:
    a1, b1, b2 = _mutant("a1"), _mutant("b1"), _mutant("b2")
    plan = plan_mutants({"b.py": [b1, b2], "a.py": [a1]}, budget=3)
    assert plan == (("a.py", a1), ("b.py", b1), ("b.py", b2))


def test_plan_mutants_respects_budget_and_exhaustion() -> None:
    a1, b1, b2 = _mutant("a1"), _mutant("b1"), _mutant("b2")
    per_file = {"a.py": [a1], "b.py": [b1, b2]}
    assert plan_mutants(per_file, budget=2) == (("a.py", a1), ("b.py", b1))
    assert plan_mutants(per_file, budget=10) == (("a.py", a1), ("b.py", b1), ("b.py", b2))
    assert plan_mutants(per_file, budget=0) == ()
    assert plan_mutants({}, budget=5) == ()


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


def _entry(file: str, line: int, killed_selected: bool, killed_full: bool) -> dict[str, Any]:
    return {
        "file": file,
        "line": line,
        "col": 4,
        "kind": "boundary",
        "description": "1 to 2",
        "killed_by_selected": killed_selected,
        "killed_by_full": killed_full,
        "selected_seconds": 1.0,
        "full_seconds": 2.0,
    }


def _block(entries: list[dict[str, Any]]) -> dict[str, Any]:
    kills = [(e["killed_by_selected"], e["killed_by_full"]) for e in entries]
    return {
        "requested": 4,
        "entries": entries,
        "errors": [],
        "detection_parity": detection_parity(kills),
    }


def _write(directory: Path, name: str, payload: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def test_aggregate_renders_not_run_without_mutant_data(tmp_path: Path) -> None:
    _write(tmp_path / "results", "pr-000001.json", _result(1))
    out = tmp_path / "summary.md"
    assert run_aggregate(tmp_path / "results", out) == 0
    markdown = out.read_text(encoding="utf-8")
    assert "## Mutation arm" in markdown
    assert "Not run." in markdown
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["mutation_arm"] == {
        "prs_with_mutants": 0,
        "mutants": 0,
        "killed_by_full": 0,
        "missed": [],
        "parity": None,
    }


def test_aggregate_reports_mutation_arm_numbers(tmp_path: Path) -> None:
    results = tmp_path / "results"
    _write(
        results,
        "pr-000001.json",
        _result(1, mutants=_block([_entry("src/a.py", 3, True, True)])),
    )
    _write(
        results,
        "pr-000002.json",
        _result(
            2,
            mutants=_block(
                [_entry("src/b.py", 5, True, True), _entry("src/b.py", 9, False, False)]
            ),
        ),
    )
    _write(results, "pr-000003.json", _result(3))
    loaded, exclusions = load_results(results)
    summary = summarize(loaded, exclusions)
    assert summary.mutant_prs == 2
    assert summary.mutant_total == 3
    assert summary.mutant_killed_by_full == 2
    assert summary.mutant_missed == ()
    assert summary.mutant_parity is not None
    assert summary.mutant_parity.median == pytest.approx(1.0)
    out = tmp_path / "summary.md"
    assert run_aggregate(results, out) == 0
    markdown = out.read_text(encoding="utf-8")
    assert "- PRs with injected mutants: 2" in markdown
    assert "- Mutants run: 3" in markdown
    assert "- Killed by the full suite: 2" in markdown
    assert "- Missed by the selected set: 0 (must be 0)" in markdown


def test_aggregate_fails_on_missed_mutant(tmp_path: Path) -> None:
    results = tmp_path / "results"
    _write(
        results,
        "pr-000009.json",
        _result(
            9,
            mutants=_block(
                [
                    _entry("src/pkg/mod.py", 12, False, True),
                    _entry("src/pkg/mod.py", 20, True, True),
                ]
            ),
        ),
    )
    out = tmp_path / "summary.md"
    assert run_aggregate(results, out) == 1
    markdown = out.read_text(encoding="utf-8")
    assert "- Missed by the selected set: 1 (must be 0)" in markdown
    assert "| 9 | src/pkg/mod.py | 12:4 | boundary |" in markdown
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["mutation_arm"]["missed"] == [
        {"pr": 9, "file": "src/pkg/mod.py", "line": 12, "col": 4, "kind": "boundary"}
    ]


def test_missed_mutant_does_not_mask_unsafe_skip_failure(tmp_path: Path) -> None:
    results = tmp_path / "results"
    _write(
        results,
        "pr-000009.json",
        _result(
            9,
            unsafe_skips=["tests/test_a.py"],
            skip_paths=["tests/test_a.py"],
            mutants=_block([_entry("src/a.py", 3, False, True)]),
        ),
    )
    assert run_aggregate(results, tmp_path / "summary.md") == 1


def test_parity_distribution_over_prs(tmp_path: Path) -> None:
    results = tmp_path / "results"
    _write(
        results,
        "pr-000001.json",
        _result(1, mutants=_block([_entry("src/a.py", 1, True, True)])),
    )
    _write(
        results,
        "pr-000002.json",
        _result(
            2,
            mutants=_block([_entry("src/b.py", 1, True, True), _entry("src/b.py", 2, False, True)]),
        ),
    )
    loaded, exclusions = load_results(results)
    summary = summarize(loaded, exclusions)
    assert summary.mutant_parity is not None
    assert summary.mutant_parity.median == pytest.approx(0.75)
