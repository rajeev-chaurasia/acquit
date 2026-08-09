"""Tests for the explain command: every decision names its evidence."""

import shutil
from pathlib import Path

import pytest
from conftest import ScenarioRepo, commit_all, init_repo, module_test_source, write_files

from acquit.cli import main
from acquit.errors import ExitCode
from acquit.explain import explain_lines
from acquit.pipeline import SelectResult, run_select
from acquit.witness import CLAIM_DISJOINT, CLAIM_NARROWED

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")


@pytest.fixture(scope="module")
def chain_result(tmp_path_factory: pytest.TempPathFactory) -> SelectResult:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    repo = init_repo(tmp_path_factory.mktemp("chain"))
    write_files(
        repo,
        {
            "leaf.py": "LEAF = 1\n",
            "mid.py": "import leaf\n",
            "dyn.py": (
                "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n"
            ),
            "other.py": "OTHER = 1\n",
            "tests/test_chain.py": module_test_source("mid"),
            "tests/test_dyn.py": module_test_source("dyn"),
            "tests/test_other.py": module_test_source("other"),
        },
    )
    base = commit_all(repo, "base")
    write_files(repo, {"leaf.py": "LEAF = 2\n"})
    head = commit_all(repo, "change leaf")
    return run_select(base, head, repo)


def test_explain_selected_prints_shortest_dependency_chain(chain_result: SelectResult) -> None:
    lines, code = explain_lines("tests/test_chain.py", chain_result)

    assert code == ExitCode.OK
    assert lines[0] == "tests/test_chain.py: selected"
    assert "  reason: reachable-from:leaf.py" in lines
    assert "    tests/test_chain.py imports mid.py imports leaf.py" in lines


def test_explain_skipped_prints_witness_details(chain_result: SelectResult) -> None:
    lines, code = explain_lines("tests/test_other.py", chain_result)

    assert code == ExitCode.OK
    assert lines[0] == "tests/test_other.py: skipped"
    assert any(line.startswith("  witness: w-") for line in lines)
    assert f"  claim: {CLAIM_DISJOINT}" in lines
    assert any(line.startswith("  closure: 2 files, hash ") for line in lines)
    assert "  changed: leaf.py" in lines


@pytest.fixture(scope="module")
def narrowed_result(tmp_path_factory: pytest.TempPathFactory) -> SelectResult:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    repo = init_repo(tmp_path_factory.mktemp("narrowed-explain"))
    write_files(
        repo,
        {
            ".acquit.toml": "narrowing = true\n",
            "pkg/__init__.py": (
                '"""Pure re-exporter."""\n\nfrom .console import Console\n'
                'from .table import Table\n\n__all__ = ["Console", "Table"]\n'
            ),
            "pkg/table.py": (
                '"""Inert sibling."""\n\n\nclass Table:\n'
                '    def render(self) -> str:\n        return "table"\n'
            ),
            "pkg/console.py": (
                '"""Not inert."""\n\nSTATE = dict(fancy="*")\n\n\nclass Console:\n    pass\n'
            ),
            "test_console.py": (
                "from pkg import Console\n\n\ndef test_console():\n    assert True\n"
            ),
            "test_table.py": "from pkg import Table\n\n\ndef test_table():\n    assert Table\n",
        },
    )
    base = commit_all(repo, "base")
    write_files(
        repo,
        {
            "pkg/table.py": (
                '"""Inert sibling."""\n\n\nclass Table:\n'
                '    def render(self) -> str:\n        return "grid"\n'
            )
        },
    )
    head = commit_all(repo, "edit inert sibling body")
    return run_select(base, head, repo)


def test_explain_narrowed_skip_renders_the_witness_block(narrowed_result: SelectResult) -> None:
    lines, code = explain_lines("test_console.py", narrowed_result)

    assert code == ExitCode.OK
    assert lines[0] == "test_console.py: skipped"
    assert f"  claim: {CLAIM_NARROWED}" in lines
    assert "  narrowed: 1 import-time-only file(s)" in lines
    assert any(
        line.startswith("    pkg/table.py: blob ") and "via pkg/__init__.py [strict]" in line
        for line in lines
    )


def test_explain_renders_narrowing_refused_reasons(narrowed_result: SelectResult) -> None:
    lines, code = explain_lines("test_table.py", narrowed_result)

    assert code == ExitCode.OK
    assert lines[0] == "test_table.py: selected"
    assert "  reason: narrowing-refused:inside-semantic-closure" in lines


def test_explain_always_run_prints_the_finding(chain_result: SelectResult) -> None:
    lines, code = explain_lines("tests/test_dyn.py", chain_result)

    assert code == ExitCode.OK
    assert lines == ("tests/test_dyn.py: always runs", "  finding: R007:dyn.py")


def test_explain_unknown_path_is_a_usage_error(chain_result: SelectResult) -> None:
    lines, code = explain_lines("tests/nope.py", chain_result)
    assert code == ExitCode.USAGE
    assert "not a known test file" in lines[0]

    lines, code = explain_lines("mid.py", chain_result)
    assert code == ExitCode.USAGE


def test_explain_global_run_prints_global_reasons(scenario_repo: ScenarioRepo) -> None:
    result = run_select(
        scenario_repo.conftest_change, scenario_repo.manifest_change, scenario_repo.path
    )

    lines, code = explain_lines("tests/test_alpha.py", result)

    assert code == ExitCode.OK
    assert lines[0] == "tests/test_alpha.py: runs; global findings force the full suite:"
    assert any(line.startswith("  R002 pyproject.toml:") for line in lines)


def test_explain_escalated_mutator_names_the_reached_subject(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    write_files(
        repo,
        {
            "paths.py": "import sys\n\nsys.path.append('vendor')\n",
            "other.py": "OTHER = 1\n",
            "tests/test_paths.py": module_test_source("paths"),
            "tests/test_other.py": module_test_source("other"),
        },
    )
    base = commit_all(repo, "base")
    write_files(repo, {"other.py": "OTHER = 2\n"})
    head = commit_all(repo, "change other")
    result = run_select(base, head, repo)

    lines, code = explain_lines("tests/test_other.py", result)

    assert code == ExitCode.OK
    assert lines[0] == "tests/test_other.py: runs; global findings force the full suite:"
    assert any(line.startswith("  R008 paths.py (escalated: a test reaches it):") for line in lines)


def test_explain_cli_prints_to_stdout_and_signals_usage(
    scenario_repo: ScenarioRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(scenario_repo.path)
    refs = ["--base", scenario_repo.base, "--head", scenario_repo.alpha_change]

    assert main(["explain", "tests/test_alpha.py", *refs]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "tests/test_alpha.py imports alpha.py" in out

    assert main(["explain", "tests/missing.py", *refs]) == ExitCode.USAGE
    assert "not a known test file" in capsys.readouterr().err
