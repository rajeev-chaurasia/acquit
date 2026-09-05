import json
import shutil

import pytest
from conftest import init_repo

from acquit.constants import CANARY_SCHEMA, ENV_CANARY, ENV_SELECTION_FILE, SELECTION_SCHEMA
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


def test_canary_alarms_on_a_failing_would_be_skipped_file(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(
        test_alpha="def test_alpha():\n    assert False\n",
        test_beta="def test_beta():\n    assert True\n",
    )
    fingerprint = _git_tree(pytester)
    selection = _write_selection(pytester, "selective", ["test_alpha.py"], fingerprint)
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    monkeypatch.setenv(ENV_CANARY, "1")
    result = pytester.runpytest_inprocess()
    # Nothing was deselected, and the failure keeps its own exit status.
    result.assert_outcomes(passed=1, failed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "*acquit canary: ALARM: test_alpha.py failed but was provably unaffected"
            " (witness w-000001)*",
            "*acquit canary: 1 alarm(s) across 1 would-be-skipped files*",
        ]
    )
    verdict = json.loads((pytester.path / "selection.canary.json").read_text(encoding="utf-8"))
    assert verdict["schema"] == CANARY_SCHEMA
    assert verdict["selection"]["status"] == "verified"
    assert verdict["stats"] == {
        "collected": 2,
        "passed": 1,
        "failed": 1,
        "skipped": 0,
        "would_skip": 1,
    }
    assert verdict["shadow_validation"] == {
        "status": "missed-impact",
        "missed_impact": [
            {
                "severity": "high",
                "path": "test_alpha.py",
                "witness": "w-000001",
                "nodes": ["test_alpha.py::test_alpha"],
            }
        ],
    }
    assert (
        (pytester.path / "selection.canary.md")
        .read_text(encoding="utf-8")
        .startswith("# Acquit canary evidence\n")
    )


def test_canary_clean_when_every_would_be_skipped_file_passes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    fingerprint = _git_tree(pytester)
    selection = _write_selection(pytester, "selective", ["test_alpha.py"], fingerprint)
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    monkeypatch.setenv(ENV_CANARY, "1")
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        ["*acquit canary: clean: all 1 would-be-skipped files passed (selection validated live)*"]
    )
    verdict = json.loads((pytester.path / "selection.canary.json").read_text(encoding="utf-8"))
    assert verdict["schema"] == CANARY_SCHEMA
    assert verdict["selection"]["status"] == "verified"
    assert verdict["shadow_validation"] == {"status": "clean", "missed_impact": []}


def test_canary_ignores_failures_outside_the_skip_set(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(
        test_alpha="def test_alpha():\n    assert True\n",
        test_beta="def test_beta():\n    assert False\n",
    )
    fingerprint = _git_tree(pytester)
    selection = _write_selection(pytester, "selective", ["test_alpha.py"], fingerprint)
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    monkeypatch.setenv(ENV_CANARY, "1")
    result = pytester.runpytest_inprocess()
    # beta was selected to run and failed; that is the selection working.
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*acquit canary: clean: all 1 would-be-skipped files passed*"])
    verdict = json.loads((pytester.path / "selection.canary.json").read_text(encoding="utf-8"))
    assert verdict["stats"]["failed"] == 1
    assert verdict["shadow_validation"] == {"status": "clean", "missed_impact": []}


def test_canary_stale_fingerprint_refuses_without_claims(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    fingerprint = _git_tree(pytester)
    selection = _write_selection(pytester, "selective", ["test_alpha.py"], fingerprint)
    (pytester.path / "test_beta.py").write_text(
        "def test_beta():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    monkeypatch.setenv(ENV_CANARY, "1")
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*running every test*"])
    assert "acquit canary" not in result.stdout.str()
    verdict = json.loads((pytester.path / "selection.canary.json").read_text(encoding="utf-8"))
    assert verdict["selection"]["status"] == "refused"
    assert "tree fingerprint mismatch" in verdict["selection"]["reason"]
    assert verdict["shadow_validation"]["status"] == "incomplete"


def test_canary_run_all_document_has_nothing_to_validate(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(**TEST_FILES)
    selection = _write_selection(pytester, "run-all", [], None)
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    monkeypatch.setenv(ENV_CANARY, "1")
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*acquit canary: selection was run-all, nothing to validate*"])
    verdict = json.loads((pytester.path / "selection.canary.json").read_text(encoding="utf-8"))
    assert verdict["selection"]["status"] == "run-all"
    assert verdict["shadow_validation"]["status"] == "incomplete"


def test_canary_records_runtime_imports_and_fixture_providers(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest(
        "import pytest\n"
        "@pytest.fixture\n"
        "def helper():\n"
        "    import support\n"
        "    return support.VALUE\n"
    )
    pytester.makepyfile(
        support="VALUE = 1\n",
        dynamic="VALUE = 2\n",
        test_alpha=(
            "import importlib\n"
            "def test_alpha(helper):\n"
            "    assert helper == 1\n"
            "    assert importlib.import_module('dynamic').VALUE == 2\n"
        ),
    )
    fingerprint = _git_tree(pytester)
    selection = _write_selection(pytester, "selective", ["test_alpha.py"], fingerprint)
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    monkeypatch.setenv(ENV_CANARY, "1")

    result = pytester.runpytest_inprocess()

    result.assert_outcomes(passed=1)
    verdict = json.loads((pytester.path / "selection.canary.json").read_text(encoding="utf-8"))
    (observation,) = verdict["tests"]
    assert observation["nodeid"] == "test_alpha.py::test_alpha"
    assert observation["selection"] == {
        "source": "static",
        "classification": "would-skip",
        "witness": "w-000001",
    }
    assert observation["dependencies"]["modules"] == [
        {"path": "dynamic.py", "kind": "runtime-import"},
        {"path": "support.py", "kind": "runtime-import"},
    ]
    assert {entry["provider"] for entry in observation["dependencies"]["fixtures"]} == {
        "conftest.py"
    }
    assert {entry["module"] for entry in verdict["observations"]["runtime_edges"]} == {
        "dynamic.py",
        "support.py",
    }


def test_canary_flag_other_than_one_still_deselects(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enforce behavior is untouched unless the flag is a literal 1."""
    pytester.makepyfile(**TEST_FILES)
    fingerprint = _git_tree(pytester)
    selection = _write_selection(pytester, "selective", ["test_alpha.py"], fingerprint)
    monkeypatch.setenv(ENV_SELECTION_FILE, selection)
    monkeypatch.setenv(ENV_CANARY, "0")
    result = pytester.runpytest_inprocess()
    result.assert_outcomes(passed=1, deselected=1)
    assert "acquit canary" not in result.stdout.str()


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
