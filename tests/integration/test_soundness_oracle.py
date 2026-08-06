"""The runtime soundness oracle.

For every fixture repo, real pytest runs in a subprocess with a recording
plugin that observes which repo-relative first-party files each collected test
file actually imports. The static promise under test: import_closure(test) is
a superset of the runtime observation. The dynamic fixture is the documented
exception: its tainted modules import things no static analysis can see, so
there the oracle instead proves that every test reaching a tainted module is
forced to run. The doctest fixture must refuse selective mode outright.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fixtures.conftest import (
    FIXTURE_NAMES,
    FixtureRepo,
    build_fixture_repo,
    snapshot_working_tree_plain,
)

from acquit.graph.model import BuiltGraph, NodeKind
from acquit.pipeline import SelectResult, run_select
from acquit.policy.model import RuleId
from acquit.report import SelectionMode
from acquit.select import import_closure
from integration.conftest import RECORDER_MODULE, RECORDER_SOURCE

pytestmark = [
    pytest.mark.oracle,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not available"),
]

_DYNAMIC_TAINTED = frozenset({"dynloader.py", "execmod.py", "lazy.py"})
_DYNAMIC_REACHERS = frozenset({"tests/test_dyn.py", "tests/test_exec.py", "tests/test_lazy.py"})


@pytest.fixture(scope="module")
def recorder_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("recorder")
    (directory / f"{RECORDER_MODULE}.py").write_text(
        RECORDER_SOURCE, encoding="utf-8", newline="\n"
    )
    return directory


@pytest.fixture(scope="module")
def oracle_repo(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], FixtureRepo]:
    built: dict[str, FixtureRepo] = {}

    def get(name: str) -> FixtureRepo:
        if name not in built:
            built[name] = build_fixture_repo(name, tmp_path_factory.mktemp(f"oracle-{name}"))
        return built[name]

    return get


def _run_recorded_pytest(repo: Path, recorder_dir: Path) -> dict[str, Any]:
    out = recorder_dir / f"record-{repo.name}.json"
    roots = [str(recorder_dir), str(repo)]
    if (repo / "src").is_dir():
        roots.append(str(repo / "src"))
    env = dict(os.environ)
    env.pop("ACQUIT_SELECTION_FILE", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(roots)
    env["ACQUIT_ORACLE_REPO"] = str(repo)
    env["ACQUIT_ORACLE_OUT"] = str(out)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", RECORDER_MODULE, "-p", "no:cacheprovider"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    record: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    return record


@contextmanager
def _touched(repo: Path, relative: str) -> Iterator[None]:
    target = repo / relative
    original = target.read_text(encoding="utf-8")
    target.write_text(original + "# touched by oracle\n", encoding="utf-8", newline="\n")
    try:
        yield
    finally:
        target.write_text(original, encoding="utf-8", newline="\n")


def _buckets(result: SelectResult) -> tuple[set[str], set[str], set[str]]:
    return (
        {entry.path for entry in result.decision.selected},
        {entry.path for entry in result.decision.skipped},
        {entry.path for entry in result.decision.always_run},
    )


def _assert_doctest_refuses_selection(built: FixtureRepo) -> None:
    with _touched(built.path, "mathy.py"):
        result = run_select(built.base_sha, None, built.path)
    assert result.decision.mode is SelectionMode.RUN_ALL
    assert RuleId.DOCTEST_MODULES in {finding.rule for finding in result.outcome.findings}
    assert result.decision.skipped == ()


def _assert_dynamic_taint_covers_runtime(
    built: FixtureRepo, graph: BuiltGraph, per_file: dict[str, set[str]]
) -> None:
    tainted = {path for path, node in graph.nodes.items() if node.tainted}
    assert tainted == _DYNAMIC_TAINTED

    runtime_reachers = {test for test, files in per_file.items() if files & tainted}
    assert runtime_reachers == _DYNAMIC_REACHERS

    # The gap is real: these files are imported at runtime yet absent from
    # the static closures. This is exactly what taint has to compensate for.
    assert "plugins_extra.py" in per_file["tests/test_dyn.py"]
    assert "plugins_extra.py" not in import_closure(graph, "tests/test_dyn.py")
    assert "exec_target.py" in per_file["tests/test_exec.py"]
    assert "exec_target.py" not in import_closure(graph, "tests/test_exec.py")

    # Tests that avoid the tainted modules still satisfy the closure bound.
    for test in sorted(set(per_file) - runtime_reachers):
        assert per_file[test] <= import_closure(graph, test), test

    # A change visible only through runtime behavior never skips a reacher.
    with _touched(built.path, "plugins_extra.py"):
        result = run_select(built.base_sha, None, built.path)
    selected, skipped, always = _buckets(result)
    assert not runtime_reachers & skipped
    assert runtime_reachers <= selected | always


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_static_closures_bound_runtime_imports(
    name: str,
    oracle_repo: Callable[[str], FixtureRepo],
    recorder_dir: Path,
) -> None:
    built = oracle_repo(name)
    snapshot = snapshot_working_tree_plain(built.path)
    graph = snapshot.graph
    static_tests = {path for path, node in graph.nodes.items() if node.kind is NodeKind.TEST}

    if name == "doctest_mode":
        _assert_doctest_refuses_selection(built)
        return

    record = _run_recorded_pytest(built.path, recorder_dir)
    per_file = {test: set(files) for test, files in record["per_file"].items()}
    union = set(record["union"])

    # Static discovery predicted exactly the files pytest collected.
    assert set(per_file) == static_tests

    if name == "dynamic":
        _assert_dynamic_taint_covers_runtime(built, graph, per_file)
        return

    for test, runtime in sorted(per_file.items()):
        assert runtime <= import_closure(graph, test), test
    closure_union: set[str] = set()
    for test in static_tests:
        closure_union |= import_closure(graph, test)
    assert union <= closure_union
