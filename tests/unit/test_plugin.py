import json
import shutil

import pytest
from conftest import init_repo

from acquit.constants import ENV_SELECTION_FILE, SELECTION_SCHEMA
from acquit.vcs import working_tree_fingerprint

pytest_plugins = ["pytester"]

TEST_FILES = {
    "test_alpha.py": "def test_alpha():\n    assert True\n",
    "test_beta.py": "def test_beta():\n    assert True\n",
}

# Keeps the tree fingerprint stable across the write and the inner pytest run.
GITIGNORE = "selection.json\n__pycache__/\n*.pyc\n.pytest_cache/\n"


def _write_selection(
    pytester: pytest.Pytester,
    mode: str,
    skip: list[str],
    fingerprint: str | None,
    name: str = "selection.json",
    artifacts: dict[str, str | None] | None = None,
) -> str:
    document: dict[str, object] = {
        "schema": SELECTION_SCHEMA,
        "mode": mode,
        "graph_hash": "0" * 64,
        "tree": {"head_sha": None, "fingerprint": fingerprint},
        "skip": [{"path": path, "witness": f"w-{index:06d}"} for index, path in enumerate(skip, 1)],
    }
    if artifacts is not None:
        document["artifacts"] = artifacts
    path = pytester.path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _git_tree(pytester: pytest.Pytester) -> str:
    """git-init the pytester dir and return its working tree fingerprint."""
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    (pytester.path / ".gitignore").write_text(GITIGNORE, encoding="utf-8", newline="\n")
    init_repo(pytester.path)
    return working_tree_fingerprint(pytester.path)


def test_no_env_var_runs_everything(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(**TEST_FILES)
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=2)


def test_selective_mode_deselects_listed_files(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    fingerprint = _git_tree(pytester)
    selection = _write_selection(pytester, "selective", ["test_alpha.py"], fingerprint)
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=1, deselected=1)


def test_stale_fingerprint_refuses_the_document(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    fingerprint = _git_tree(pytester)
    selection = _write_selection(pytester, "selective", ["test_alpha.py"], fingerprint)
    (pytester.path / "test_beta.py").write_text(
        "def test_beta():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*running every test*"])


def test_the_selection_file_itself_is_exempt_from_the_fingerprint(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    fingerprint = _git_tree(pytester)
    # Not gitignored: without the self-exemption this write drifts the tree.
    selection = _write_selection(
        pytester, "selective", ["test_alpha.py"], fingerprint, name="acquit-selection.json"
    )
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=1, deselected=1)


def test_recorded_artifacts_are_exempt_from_the_fingerprint(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    fingerprint = _git_tree(pytester)
    (pytester.path / "acquit-report.json").write_text("{}", encoding="utf-8")
    selection = _write_selection(
        pytester,
        "selective",
        ["test_alpha.py"],
        fingerprint,
        artifacts={"report": "acquit-report.json", "selection": None, "witnesses": None},
    )
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=1, deselected=1)


def test_missing_selection_file_runs_everything_without_warning(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    monkeypatch.setenv(ENV_SELECTION_FILE, str(pytester.path / "missing.json"))
    # Only the rewrite noise of already-imported plugins is filtered out here;
    # acquit itself must stay silent in the warnings system (ADV-FC-4).
    result = pytester.runpytest_inprocess("-W", "ignore::pytest.PytestAssertRewriteWarning")
    result.assert_outcomes(passed=2, warnings=0)
    result.stdout.fnmatch_lines(["*acquit: selection*refused*running every test*"])


def test_run_all_mode_deselects_nothing(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    selection = _write_selection(pytester, "run-all", [], None)
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=2)


def test_oversized_selection_file_is_refused(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    bloated = pytester.path / "selection.json"
    bloated.write_text(" " * (5 * 1024 * 1024 + 1), encoding="utf-8")
    monkeypatch.setenv(ENV_SELECTION_FILE, str(bloated))
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*too large*running every test*"])
