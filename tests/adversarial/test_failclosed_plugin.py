"""Attacks on the delivery layer: the selection document and the pytest plugin.

The plugin is the last hop between a proof and a test run. It accepts any
document that parses, applies it to whatever tree it happens to find, and has
no way to tell a fresh proof from a stale one. Every reproduction here runs
real pytest in a subprocess so the installed plugin, not a stub, does the
deselecting.
"""

import json
import os
from pathlib import Path

import pytest

from adversarial.failclosed_support import (
    commit,
    deselected_count,
    outcome,
    read_json,
    run_pytest,
    select,
    selection_doc,
    two_module_repo,
    write,
    write_json,
)

SIMPLE_TESTS = {
    "tests/test_a.py": "def test_a():\n    assert True\n",
    "tests/test_b.py": "def test_b():\n    assert True\n",
}


def simple_project(path: Path) -> Path:
    """Two independent test files, no git, no imports."""
    path.mkdir(parents=True, exist_ok=True)
    for name, content in SIMPLE_TESTS.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return path


@pytest.mark.xfail(
    strict=True,
    reason="ADV-FC-1: the plugin never checks that a selection document describes the tree it "
    "is applied to, so a stale document silently deselects an impacted test",
)
def test_stale_selection_deselects_a_test_the_fresh_analysis_selects(tmp_path: Path) -> None:
    repo = two_module_repo(tmp_path / "repo")
    out = tmp_path / "out"
    base = commit(repo, "base")
    write(repo, {"alpha.py": "ALPHA = 2\n"})
    alpha_change = commit(repo, "change alpha")

    assert select(repo, out, base, alpha_change) == 0
    stale = out / "selection.json"
    assert read_json(stale)["skip"] == ["tests/test_beta.py"]

    # A retried job, a cached workspace, or a rebase moves the tree on; the
    # freshly computed selection for this tree is the exact complement.
    write(repo, {"beta.py": "BETA = 99\n"})
    beta_change = commit(repo, "change beta")
    fresh = tmp_path / "fresh"
    assert select(repo, fresh, alpha_change, beta_change) == 0
    assert read_json(fresh / "selection.json")["skip"] == ["tests/test_alpha.py"]

    result = run_pytest(repo, stale)

    assert deselected_count(result) == 0, outcome(result)


def test_stale_selection_is_undetectable_from_the_graph_hash(tmp_path: Path) -> None:
    """The one provenance field a selection carries cannot detect content drift."""
    repo = two_module_repo(tmp_path / "repo")
    base = commit(repo, "base")
    write(repo, {"alpha.py": "ALPHA = 2\n"})
    alpha_change = commit(repo, "change alpha")
    write(repo, {"beta.py": "BETA = 99\n"})
    beta_change = commit(repo, "change beta")

    first, second = tmp_path / "first", tmp_path / "second"
    assert select(repo, first, base, alpha_change) == 0
    assert select(repo, second, alpha_change, beta_change) == 0

    before = read_json(first / "selection.json")
    after = read_json(second / "selection.json")
    assert before["skip"] != after["skip"]
    # Same nodes, same edges, different file contents: the hash cannot tell.
    assert before["graph_hash"] == after["graph_hash"]


@pytest.mark.xfail(
    strict=True,
    reason="ADV-FC-2: skip entries are matched against config.rootpath, so a selection computed "
    "for one project root deselects same-named tests under a different root",
)
def test_selection_from_a_different_project_root_deselects_by_name(tmp_path: Path) -> None:
    simple_project(tmp_path / "other")
    project = simple_project(tmp_path / "project")
    # Computed for tmp_path/other, exported job-wide through GITHUB_ENV.
    selection = write_json(tmp_path / "selection.json", selection_doc(["tests/test_a.py"]))

    result = run_pytest(project, selection)

    assert deselected_count(result) == 0, outcome(result)


@pytest.mark.xfail(
    strict=True,
    reason="ADV-FC-3: item paths are resolved but config.rootpath is not, so any rootdir that "
    "is not a literal prefix of the collected files aborts collection",
)
def test_rootdir_outside_the_collected_tree_still_runs_every_test(tmp_path: Path) -> None:
    project = simple_project(tmp_path / "project")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    selection = write_json(tmp_path / "selection.json", selection_doc(["tests/test_a.py"]))

    result = run_pytest(project, selection, f"--rootdir={elsewhere}")

    assert result.returncode == 0, outcome(result) + result.stderr[-2000:]
    assert "2 passed" in result.stdout, outcome(result)


@pytest.mark.xfail(
    strict=True,
    reason="ADV-FC-4: the degraded path warns through the warnings system, so a project with "
    "filterwarnings=error turns a missing selection file into a collection abort",
)
def test_missing_selection_under_filterwarnings_error_runs_everything(tmp_path: Path) -> None:
    project = simple_project(tmp_path / "project")

    result = run_pytest(project, tmp_path / "absent.json", "-W", "error")

    assert result.returncode == 0, outcome(result) + result.stderr[-2000:]
    assert "2 passed" in result.stdout, outcome(result)


@pytest.mark.xfail(
    strict=True,
    reason="ADV-FC-5: a test file named explicitly on the command line is deselected when the "
    "selection lists it, so an operator asking for one test silently gets none",
)
def test_explicitly_requested_test_file_still_runs(tmp_path: Path) -> None:
    project = simple_project(tmp_path / "project")
    selection = write_json(tmp_path / "selection.json", selection_doc(["tests/test_a.py"]))

    result = run_pytest(project, selection, "tests/test_a.py", "tests/test_b.py")

    assert result.returncode == 0, outcome(result)
    assert deselected_count(result) == 0, outcome(result)


@pytest.mark.xfail(
    strict=True,
    reason="ADV-FC-6: _load_selection catches only OSError and ValueError, so a deeply nested "
    "selection document raises RecursionError out of the collection hook",
)
def test_deeply_nested_selection_json_runs_everything(tmp_path: Path) -> None:
    project = simple_project(tmp_path / "project")
    selection = tmp_path / "selection.json"
    selection.write_text("[" * 60000 + "]" * 60000, encoding="utf-8")

    result = run_pytest(project, selection, "-W", "ignore::pytest.PytestWarning")

    assert result.returncode == 0, outcome(result) + result.stderr[-2000:]
    assert "2 passed" in result.stdout, outcome(result)


HOSTILE_SKIP_ENTRIES = {
    "absolute": "{root}/tests/test_a.py",
    "dot-slash": "./tests/test_a.py",
    "parent-traversal": "../project/tests/test_a.py",
    "inner-traversal": "tests/../tests/test_a.py",
    "backslash": "tests\\test_a.py",
    "case-folded": "Tests/Test_A.py",
    "trailing-space": "tests/test_a.py ",
    "directory": "tests",
    "url-escaped": "tests%2Ftest_a.py",
}


@pytest.mark.parametrize("shape", sorted(HOSTILE_SKIP_ENTRIES))
def test_hostile_skip_entry_shapes_never_deselect(tmp_path: Path, shape: str) -> None:
    """Only the canonical repo-relative posix form may remove a test."""
    project = simple_project(tmp_path / "project")
    entry = HOSTILE_SKIP_ENTRIES[shape].format(root=project.as_posix())
    selection = write_json(tmp_path / "selection.json", selection_doc([entry]))

    result = run_pytest(project, selection)

    assert result.returncode == 0, outcome(result)
    assert deselected_count(result) == 0, outcome(result)


def test_canonical_skip_entry_deselects_exactly_one_file(tmp_path: Path) -> None:
    project = simple_project(tmp_path / "project")
    selection = write_json(tmp_path / "selection.json", selection_doc(["tests/test_a.py"]))

    result = run_pytest(project, selection)

    assert result.returncode == 0, outcome(result)
    assert deselected_count(result) == 1, outcome(result)
    assert "1 passed" in result.stdout, outcome(result)


def test_non_ascii_skip_entry_never_deselects_another_file(tmp_path: Path) -> None:
    """Filesystem unicode normalization may cost a skip, never a wrong one."""
    project = simple_project(tmp_path / "project")
    target = project / "tests" / "test_ünicode.py"
    target.write_text("def test_u():\n    assert True\n", encoding="utf-8", newline="\n")
    selection = write_json(tmp_path / "selection.json", selection_doc(["tests/test_ünicode.py"]))

    result = run_pytest(project, selection)

    assert result.returncode == 0, outcome(result)
    deselected = deselected_count(result)
    assert deselected in (0, 1), outcome(result)
    assert f"{3 - deselected} passed" in result.stdout, outcome(result)


def test_selection_file_that_is_a_directory_runs_everything(tmp_path: Path) -> None:
    project = simple_project(tmp_path / "project")
    directory = tmp_path / "selection.json"
    directory.mkdir()

    result = run_pytest(project, directory)

    assert result.returncode == 0, outcome(result)
    assert "2 passed" in result.stdout, outcome(result)
    assert "running every test" in result.stdout


def test_selection_symlink_loop_runs_everything(tmp_path: Path) -> None:
    project = simple_project(tmp_path / "project")
    loop = tmp_path / "loop.json"
    try:
        os.symlink(loop, loop)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks are not available: {error}")

    result = run_pytest(project, loop)

    assert result.returncode == 0, outcome(result)
    assert "2 passed" in result.stdout, outcome(result)


def test_empty_selection_env_var_is_inert(tmp_path: Path) -> None:
    project = simple_project(tmp_path / "project")

    result = run_pytest(project, "")

    assert result.returncode == 0, outcome(result)
    assert "2 passed" in result.stdout, outcome(result)


@pytest.mark.xfail(
    strict=True,
    reason="ADV-FC-12: when every test is provably unaffected the plugin leaves pytest with an "
    "empty run, so a perfect selective result exits 5 and turns the job red",
)
def test_deselecting_every_test_is_not_a_failed_run(tmp_path: Path) -> None:
    project = simple_project(tmp_path / "project")
    selection = write_json(
        tmp_path / "selection.json", selection_doc(["tests/test_a.py", "tests/test_b.py"])
    )

    result = run_pytest(project, selection)

    assert deselected_count(result) == 2, outcome(result)
    assert result.returncode == 0, outcome(result)


def test_selective_document_with_a_null_graph_hash_is_still_obeyed(tmp_path: Path) -> None:
    """A hand-written document carries no provenance and is applied anyway."""
    project = simple_project(tmp_path / "project")
    document = selection_doc(["tests/test_a.py"], graph_hash=None)
    selection = write_json(tmp_path / "selection.json", document)

    result = run_pytest(project, selection)

    assert deselected_count(result) == 1, outcome(result)
    assert json.loads(selection.read_text(encoding="utf-8"))["graph_hash"] is None
