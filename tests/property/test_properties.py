"""Property-based soundness checks over generated repositories.

Repositories are small package trees whose import DAG is acyclic by
construction: a module may only import modules with lexicographically earlier
dotted names. Three properties are checked against the full pipeline:

P1  every skipped test carries a witness that re-verifies, and the decision
    partitions the discovered test set exactly;
P2  mutation soundness: runtime import closures (measured in a subprocess)
    never reach the changed module from a skipped test;
P3  two pipeline runs over the same tree produce byte-identical documents.
"""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from itertools import count
from pathlib import Path

import pytest
from fixtures.conftest import commit_all, init_repo
from hypothesis import given, settings
from hypothesis import strategies as st

from acquit.graph.model import BuiltGraph, NodeKind
from acquit.pipeline import SelectResult, run_select
from acquit.report import SelectionMode, build_selection_doc, build_witnesses_doc
from acquit.select import import_closure
from acquit.witness import verify_witness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")

_DIRS = ("", "pa", "pa/inner", "qa")
_GITIGNORE = ".acquit/\n__pycache__/\n*.pyc\n"
_CASE_IDS = count()

_DRIVER_SOURCE = '''\
"""Report runtime first-party import closures for a repo's test modules."""

import importlib
import json
import sys
from pathlib import Path


def main() -> int:
    repo = Path(sys.argv[1]).resolve()
    test_paths = json.load(sys.stdin)
    closures = {}
    for test_path in test_paths:
        dotted = test_path[:-3].replace("/", ".")
        before = set(sys.modules)
        importlib.import_module(dotted)
        delta = set(sys.modules) - before
        first_party = {}
        for name in delta:
            module = sys.modules.get(name)
            filename = getattr(module, "__file__", None)
            if filename is None:
                continue
            try:
                relative = Path(filename).resolve().relative_to(repo)
            except ValueError:
                continue
            first_party[name] = relative.as_posix()
        closures[test_path] = sorted(set(first_party.values()))
        for name in first_party:
            del sys.modules[name]
    print(json.dumps(closures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


@dataclass(frozen=True)
class GeneratedRepo:
    """One generated repository: files, its test paths, and the changed module."""

    files: tuple[tuple[str, str], ...]
    tests: tuple[str, ...]
    changed: str


@st.composite
def generated_repos(draw: st.DrawFn) -> GeneratedRepo:
    module_count = draw(st.integers(min_value=2, max_value=8))
    placements = draw(
        st.lists(st.sampled_from(_DIRS), min_size=module_count, max_size=module_count)
    )
    entries: list[tuple[str, str]] = []
    for index, directory in enumerate(placements):
        path = f"{directory}/m{index}.py" if directory else f"m{index}.py"
        entries.append((path[: -len(".py")].replace("/", "."), path))
    entries.sort()

    files: list[tuple[str, str]] = [(".gitignore", _GITIGNORE)]
    for position, (_, path) in enumerate(entries):
        earlier = [dotted for dotted, _ in entries[:position]]
        deps: list[str] = []
        if earlier:
            deps = draw(st.lists(st.sampled_from(earlier), unique=True, max_size=3))
        body = "".join(f"import {dotted}\n" for dotted in sorted(deps))
        files.append((path, body + f"VALUE = {position}\n"))

    all_dotted = [dotted for dotted, _ in entries]
    test_count = draw(st.integers(min_value=1, max_value=4))
    tests: list[str] = []
    for index in range(test_count):
        deps = draw(st.lists(st.sampled_from(all_dotted), unique=True, min_size=1, max_size=3))
        path = f"tests/test_g{index}.py"
        body = "".join(f"import {dotted}\n" for dotted in sorted(deps))
        files.append((path, body + "\n\ndef test_ok():\n    assert True\n"))
        tests.append(path)

    changed = draw(st.sampled_from([path for _, path in entries]))
    return GeneratedRepo(files=tuple(files), tests=tuple(tests), changed=changed)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("property")


@pytest.fixture(scope="module")
def driver(tmp_path_factory: pytest.TempPathFactory) -> Path:
    script = tmp_path_factory.mktemp("driver") / "runtime_closures.py"
    script.write_text(_DRIVER_SOURCE, encoding="utf-8", newline="\n")
    return script


def _materialize(workspace: Path, data: GeneratedRepo) -> tuple[Path, str]:
    repo = workspace / f"case{next(_CASE_IDS)}"
    repo.mkdir()
    init_repo(repo)
    for name, content in data.files:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    base = commit_all(repo, "base")
    changed = repo / data.changed
    changed.write_text(
        changed.read_text(encoding="utf-8") + "# touched\n", encoding="utf-8", newline="\n"
    )
    return repo, base


def _buckets(result: SelectResult) -> tuple[set[str], set[str], set[str]]:
    decision = result.decision
    return (
        {entry.path for entry in decision.selected},
        {entry.path for entry in decision.skipped},
        {entry.path for entry in decision.always_run},
    )


def _test_nodes(graph: BuiltGraph) -> set[str]:
    return {path for path, node in graph.nodes.items() if node.kind is NodeKind.TEST}


def _runtime_closures(driver: Path, repo: Path, tests: tuple[str, ...]) -> dict[str, list[str]]:
    roots = [str(repo)]
    if (repo / "src").is_dir():
        roots.append(str(repo / "src"))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(roots)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        # -S skips site initialization; the driver only needs the stdlib.
        [sys.executable, "-S", str(driver), str(repo)],
        input=json.dumps(list(tests)),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    closures: dict[str, list[str]] = json.loads(completed.stdout)
    return closures


@settings(max_examples=25, deadline=None)
@given(data=generated_repos())
def test_pipeline_soundness_properties(data: GeneratedRepo, workspace: Path, driver: Path) -> None:
    """P1 (partition and witnesses), P2 (mutation soundness), P3 (determinism).

    One generated repo per example serves all three properties, because repo
    setup and pipeline runs dominate the wall time on Windows.
    """
    repo, base = _materialize(workspace, data)

    first = run_select(base, None, repo)
    second = run_select(base, None, repo)

    graph = first.head.graph
    selected, skipped, always = _buckets(first)

    # P1: selected, skipped, and always_run partition the discovered tests.
    assert selected | skipped | always == _test_nodes(graph)
    assert not (selected & skipped or selected & always or skipped & always)
    assert (first.decision.mode is SelectionMode.SELECTIVE) == bool(skipped)

    # P1: every skipped test presents a witness that re-verifies.
    by_id = {witness.id: witness for witness in first.decision.witnesses}
    assert len(by_id) == len(first.decision.skipped)
    for entry in first.decision.skipped:
        closure = import_closure(graph, entry.path)
        assert verify_witness(by_id[entry.witness_id], closure, {data.changed})

    # P2: runtime ground truth from one subprocess covering every test file.
    runtime = _runtime_closures(driver, repo, data.tests)
    assert set(runtime) == set(data.tests)
    for test, files in runtime.items():
        # The static closure over-approximates what actually gets imported.
        assert set(files) <= import_closure(graph, test)
        if data.changed in files:
            # A test that really imports the changed module never skips.
            assert test in selected | always
            assert test not in skipped

    # P3: identical trees produce byte-identical selection and witness documents.
    first_docs = (
        build_selection_doc(first.decision, graph.graph_hash, None, first.tree_fingerprint),
        build_witnesses_doc(first.decision, graph.graph_hash),
    )
    second_docs = (
        build_selection_doc(
            second.decision, second.head.graph.graph_hash, None, second.tree_fingerprint
        ),
        build_witnesses_doc(second.decision, second.head.graph.graph_hash),
    )
    for mine, theirs in zip(first_docs, second_docs, strict=True):
        assert json.dumps(mine, sort_keys=True) == json.dumps(theirs, sort_keys=True)
