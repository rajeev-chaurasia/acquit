"""Pure pieces of the study runner: install command assembly, the narrowing
arm's config injection and report harvesting, mutant targeting, and the
result payload plumbing."""

import tomllib
from pathlib import Path
from typing import Any

import pytest

from acquit.study.compare import SafetyResult
from acquit.study.manifest import PrRecord
from acquit.study.mutate import MutantTarget
from acquit.study.outcomes import SuiteOutcomes
from acquit.study.runner import (
    SelectRun,
    StepFailure,
    SuiteRun,
    inject_narrowing_config,
    install_args,
    mutant_target_for,
    narrowed_skip_paths,
    narrowed_witness_files,
    narrowing_refusals,
    restore_config,
    result_payload,
)

PYTHON = Path("wt") / ".study-venv" / "bin" / "python"
BASE = ["uv", "pip", "install", "--python", str(PYTHON), "-e", ".", "pytest"]


def test_install_args_minimal_recipe() -> None:
    assert install_args(PYTHON, (), None) == BASE


def test_install_args_appends_suite_deps_in_manifest_order() -> None:
    args = install_args(PYTHON, ("pytest<9", "attrs"), None)
    assert args == [*BASE, "pytest<9", "attrs"]


def test_install_args_puts_constraints_last() -> None:
    constraints = Path("study") / "constraints" / "flask.txt"
    args = install_args(PYTHON, ("trio",), constraints)
    assert args == [*BASE, "trio", "--constraints", str(constraints)]


def test_install_args_without_suite_deps_still_pins() -> None:
    constraints = Path("c.txt")
    args = install_args(PYTHON, (), constraints)
    assert args == [*BASE, "--constraints", str(constraints)]


WAIVED_CONFIG = (
    'roots = ["src"]\n'
    "\n"
    "[[waive]]\n"
    'rule = "R001"\n'
    'glob = "docs/**"\n'
    'justification = "docs feed no test"\n'
)


def test_inject_writes_minimal_fresh_config_and_restore_deletes_it(tmp_path: Path) -> None:
    injection = inject_narrowing_config(tmp_path)
    config = tmp_path / ".acquit.toml"
    assert config.read_text(encoding="utf-8") == "narrowing = true\n"
    restore_config(injection)
    assert not config.exists()


def test_inject_ignores_pyproject_without_acquit_section(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "target"\n'
    pyproject.write_text(original, encoding="utf-8")
    injection = inject_narrowing_config(tmp_path)
    assert (tmp_path / ".acquit.toml").read_text(encoding="utf-8") == "narrowing = true\n"
    assert pyproject.read_text(encoding="utf-8") == original
    restore_config(injection)
    assert not (tmp_path / ".acquit.toml").exists()


def test_inject_merges_into_existing_config_without_clobbering(tmp_path: Path) -> None:
    config = tmp_path / ".acquit.toml"
    config.write_text(WAIVED_CONFIG, encoding="utf-8")
    injection = inject_narrowing_config(tmp_path)
    merged = tomllib.loads(config.read_text(encoding="utf-8"))
    assert merged["narrowing"] is True
    assert merged["roots"] == ["src"]
    assert merged["waive"] == [
        {"rule": "R001", "glob": "docs/**", "justification": "docs feed no test"}
    ]
    restore_config(injection)
    assert config.read_text(encoding="utf-8") == WAIVED_CONFIG


def test_inject_leaves_a_pinned_narrowing_key_alone(tmp_path: Path) -> None:
    config = tmp_path / ".acquit.toml"
    original = "narrowing = false\n"
    config.write_text(original, encoding="utf-8")
    injection = inject_narrowing_config(tmp_path)
    assert config.read_text(encoding="utf-8") == original
    restore_config(injection)
    assert config.read_text(encoding="utf-8") == original


def test_inject_merges_into_pyproject_tool_acquit_section(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "target"\n\n[tool.acquit]\nroots = ["src"]\n'
    pyproject.write_text(original, encoding="utf-8")
    injection = inject_narrowing_config(tmp_path)
    # No .acquit.toml appears: it would shadow the section that already wins.
    assert not (tmp_path / ".acquit.toml").exists()
    merged = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert merged["tool"]["acquit"] == {"narrowing": True, "roots": ["src"]}
    restore_config(injection)
    assert pyproject.read_text(encoding="utf-8") == original


def test_inject_respects_pyproject_pinned_narrowing(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = "[tool.acquit]\nnarrowing = false\n"
    pyproject.write_text(original, encoding="utf-8")
    injection = inject_narrowing_config(tmp_path)
    assert pyproject.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".acquit.toml").exists()
    restore_config(injection)
    assert pyproject.read_text(encoding="utf-8") == original


def test_inject_rejects_unparseable_config(tmp_path: Path) -> None:
    (tmp_path / ".acquit.toml").write_text("narrowing = [unclosed\n", encoding="utf-8")
    with pytest.raises(StepFailure):
        inject_narrowing_config(tmp_path)


def test_inject_declines_dotted_tool_acquit_header(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = "[tool]\nacquit = { roots = [] }\n"
    pyproject.write_text(original, encoding="utf-8")
    with pytest.raises(StepFailure):
        inject_narrowing_config(tmp_path)
    assert pyproject.read_text(encoding="utf-8") == original


REPORT: dict[str, Any] = {
    "decision": {"mode": "selective", "findings": []},
    "stats": {"selected": 3, "skipped": 2, "always_run": 0, "total": 5},
    "tests": {
        "selected": [
            {"path": "tests/test_kept.py", "reasons": ["reachable-from:src/pkg/core.py"]},
            {"path": "tests/test_refused.py", "reasons": ["narrowing-refused:impure-init"]},
            {
                "path": "tests/test_refused_too.py",
                "reasons": ["new-test", "narrowing-refused:impure-init"],
            },
        ],
        "skipped": [
            {"path": "tests/test_plain.py", "witness": "w1"},
            {"path": "tests/test_narrowed.py", "witness": "w2", "narrowed": True},
        ],
        "always_run": [],
    },
}

WITNESSES: dict[str, Any] = {
    "witnesses": [
        {"id": "w1", "test": "tests/test_plain.py"},
        {
            "id": "w2",
            "test": "tests/test_narrowed.py",
            "narrowed": [
                {"path": "src/pkg/console.py", "inits": [{"path": "src/pkg/__init__.py"}]},
                {"path": "src/pkg/style.py", "inits": [{"path": "src/pkg/__init__.py"}]},
            ],
        },
    ]
}


def test_narrowed_skip_paths_reads_only_flagged_entries() -> None:
    assert narrowed_skip_paths(REPORT) == ("tests/test_narrowed.py",)
    assert narrowed_skip_paths({}) == ()


def test_narrowing_refusals_histogram_counts_selected_tests() -> None:
    assert narrowing_refusals(REPORT) == {"impure-init": 2}
    assert narrowing_refusals({}) == {}


def test_narrowed_witness_files_collects_block_paths() -> None:
    assert narrowed_witness_files(WITNESSES) == frozenset(
        {"src/pkg/console.py", "src/pkg/style.py"}
    )
    assert narrowed_witness_files({}) == frozenset()


def test_mutant_target_restricts_narrowed_files_only() -> None:
    narrowed = frozenset({"src/pkg/console.py"})
    restricted = mutant_target_for("src/pkg/console.py", narrowed)
    assert restricted is MutantTarget.FUNCTION_BODIES_AND_CONSTANTS
    assert mutant_target_for("src/pkg/core.py", narrowed) is MutantTarget.ALL
    assert mutant_target_for("src/pkg/console.py", frozenset()) is MutantTarget.ALL


def _suite_run() -> SuiteRun:
    return SuiteRun(outcomes=SuiteOutcomes(by_test={}, file_durations={}), seconds=1.0)


def _pr() -> PrRecord:
    return PrRecord(number=7, base_sha="base7", head_sha="head7", merge_sha=None, title="x")


def test_result_payload_records_narrowing_fields() -> None:
    select_run = SelectRun(
        report=REPORT,
        selection={},
        seconds=2.0,
        replay_verified=True,
        narrowed_skip_paths=("tests/test_narrowed.py",),
        narrowed_files=frozenset({"src/pkg/console.py", "src/pkg/style.py"}),
        narrowing_refusals={"impure-init": 2},
    )
    safety = SafetyResult(
        changed_outcomes=(),
        unsafe_skips=("tests/test_narrowed.py",),
        new_tests_selected=True,
        unsafe_narrowed_skips=("tests/test_narrowed.py",),
    )
    payload = result_payload(
        _pr(),
        _suite_run(),
        _suite_run(),
        select_run,
        ("tests/test_narrowed.py", "tests/test_plain.py"),
        safety,
        narrowing=True,
    )
    assert payload["narrowing"] is True
    assert payload["narrowed_skips"] == 1
    assert payload["narrowing_refusals"] == {"impure-init": 2}
    assert payload["unsafe_narrowed_skips"] == ["tests/test_narrowed.py"]


def test_result_payload_defaults_to_the_plain_arm() -> None:
    select_run = SelectRun(report=REPORT, selection={}, seconds=2.0, replay_verified=True)
    safety = SafetyResult(changed_outcomes=(), unsafe_skips=(), new_tests_selected=True)
    payload = result_payload(_pr(), _suite_run(), _suite_run(), select_run, (), safety)
    assert payload["narrowing"] is False
    assert payload["narrowed_skips"] == 0
    assert payload["narrowing_refusals"] == {}
    assert payload["unsafe_narrowed_skips"] == []
