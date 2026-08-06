import pytest

from acquit.constants import ENV_SELECTION_FILE

pytest_plugins = ["pytester"]

TEST_FILES = {
    "test_alpha.py": "def test_alpha():\n    assert True\n",
    "test_beta.py": "def test_beta():\n    assert True\n",
}


def _write_selection(pytester: pytest.Pytester, mode: str, skip: list[str]) -> str:
    import json

    document = {"schema": "acquit/selection-v1", "mode": mode, "skip": skip, "graph_hash": None}
    path = pytester.path / "selection.json"
    path.write_text(json.dumps(document))
    return str(path)


def test_no_env_var_runs_everything(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(**TEST_FILES)
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=2)


def test_selective_mode_deselects_listed_files(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    selection = _write_selection(pytester, mode="selective", skip=["test_alpha.py"])
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=1, deselected=1)


def test_missing_selection_file_runs_everything_with_warning(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    monkeypatch.setenv(ENV_SELECTION_FILE, str(pytester.path / "missing.json"))
    result = pytester.runpytest_inprocess("-W", "ignore::pytest.PytestAssertRewriteWarning")
    result.assert_outcomes(passed=2, warnings=1)


def test_run_all_mode_deselects_nothing(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    selection = _write_selection(pytester, mode="run-all", skip=[])
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=2)
