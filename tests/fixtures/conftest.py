"""Repo builder and loading helpers for the golden fixture suite.

Each directory under repos/ is one fixture repository stored as plain files.
Tests copy a fixture into a throwaway git repository, snapshot it, and drive
scenarios from scenarios.json. Scenario "changed" entries are plain paths for
modifications (a comment line is appended), "delete:<path>" for deletions, or
{"path", "content"} objects for full rewrites. A scenario with "narrowing"
set runs against a repo copy whose committed .acquit.toml enables ADR 0008
narrowing.
"""

import json
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pytest

from acquit.config import load_config
from acquit.graph.model import BuiltGraph
from acquit.pipeline import Snapshot, snapshot_tree
from acquit.pytestmap.pytestcfg import load_pytest_config
from acquit.vcs import ChangedFile, ChangeStatus

collect_ignore = ["repos"]

REPOS_DIR = Path(__file__).resolve().parent / "repos"
FIXTURE_NAMES = tuple(sorted(entry.name for entry in REPOS_DIR.iterdir() if entry.is_dir()))

_TOUCH_LINE = "# touched by scenario\n"
_DELETE_PREFIX = "delete:"
_NARROWING_TOML = "narrowing = true\n"


def run_git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return completed.stdout.strip()


_GIT_CONFIG = """
[user]
\tname = Acquit Fixtures
\temail = fixtures@acquit.invalid
[commit]
\tgpgsign = false
[core]
\tautocrlf = false
"""


def init_repo(path: Path) -> Path:
    run_git(path, "init", "-q", "-b", "main")
    # Appending to .git/config replaces four `git config` subprocess calls.
    with (path / ".git" / "config").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_GIT_CONFIG)
    return path


def commit_all(repo: Path, message: str) -> str:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    # The loose ref is authoritative for a repo this young; reading it saves
    # one subprocess per commit. Fall back to rev-parse if git packed it.
    ref = repo / ".git" / "refs" / "heads" / "main"
    if ref.is_file():
        return ref.read_text(encoding="utf-8").strip()
    return run_git(repo, "rev-parse", "HEAD")


@dataclass(frozen=True)
class FixtureRepo:
    """One fixture repository copied into a real git repository."""

    name: str
    path: Path
    base_sha: str


def build_fixture_repo(name: str, dst: Path, *, narrowing: bool = False) -> FixtureRepo:
    """Copy the named fixture into dst, git-init it, and commit everything.

    The metadata files (expected_graph.json, scenarios.json) describe the
    fixture and are not part of the repository under test. With narrowing,
    an .acquit.toml enabling ADR 0008 narrowing joins the base commit, so
    it is configuration, never diff content.
    """
    ignore = shutil.ignore_patterns("expected_graph.json", "scenarios.json", "__pycache__")
    shutil.copytree(REPOS_DIR / name, dst, dirs_exist_ok=True, ignore=ignore)
    if narrowing:
        (dst / ".acquit.toml").write_text(_NARROWING_TOML, encoding="utf-8", newline="\n")
    init_repo(dst)
    base_sha = commit_all(dst, "base")
    return FixtureRepo(name=name, path=dst, base_sha=base_sha)


def load_expected_graph(name: str) -> dict[str, Any]:
    raw = (REPOS_DIR / name / "expected_graph.json").read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


def load_scenarios(name: str) -> list[dict[str, Any]]:
    raw = (REPOS_DIR / name / "scenarios.json").read_text(encoding="utf-8")
    data: list[dict[str, Any]] = json.loads(raw)
    return data


def snapshot_working_tree_plain(repo: Path) -> Snapshot:
    """Snapshot the working tree without a parse cache."""
    return snapshot_tree(None, repo, load_config(repo), load_pytest_config(repo), None)


def graph_as_dict(graph: BuiltGraph) -> dict[str, Any]:
    """Canonical JSON form of a BuiltGraph, matching expected_graph.json."""
    nodes = [
        {"path": node.path, "kind": str(node.kind), "tainted": node.tainted}
        for node in sorted(graph.nodes.values(), key=lambda item: item.path)
    ]
    paths_by_index = {index: graph.digraph[index].path for index in graph.digraph.node_indices()}
    edges = sorted(
        [paths_by_index[src], paths_by_index[dst], str(kind)]
        for src, dst, kind in graph.digraph.weighted_edge_list()
    )
    return {"nodes": nodes, "edges": edges}


def apply_scenario_change(repo: Path, entries: Iterable[Any]) -> None:
    """Apply scenario changes: append a comment for path strings, write the
    given content for {"path", "content"} objects, unlink for delete: entries."""
    for entry in entries:
        if isinstance(entry, dict):
            (repo / entry["path"]).write_text(entry["content"], encoding="utf-8", newline="\n")
        elif entry.startswith(_DELETE_PREFIX):
            (repo / entry.removeprefix(_DELETE_PREFIX)).unlink()
        else:
            target = repo / entry
            content = target.read_text(encoding="utf-8")
            target.write_text(content + _TOUCH_LINE, encoding="utf-8", newline="\n")


def full_changed_paths(changed: tuple[ChangedFile, ...]) -> frozenset[str]:
    """The changed set decide() hands to build_witness: head plus base paths."""
    paths: set[str] = set()
    for change in changed:
        paths.add(change.path)
        if change.status is ChangeStatus.RENAMED and change.old_path is not None:
            paths.add(change.old_path)
    return frozenset(paths)


class RepoCache(Protocol):
    def __call__(self, name: str, *, narrowing: bool = False) -> FixtureRepo: ...


@pytest.fixture(scope="session")
def repo_cache(tmp_path_factory: pytest.TempPathFactory) -> RepoCache:
    """Session cache of built fixture repos, one throwaway git repo per variant."""
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    built: dict[tuple[str, bool], FixtureRepo] = {}

    def get(name: str, *, narrowing: bool = False) -> FixtureRepo:
        key = (name, narrowing)
        if key not in built:
            prefix = f"fixture-{name}-narrowing" if narrowing else f"fixture-{name}"
            built[key] = build_fixture_repo(
                name, tmp_path_factory.mktemp(prefix), narrowing=narrowing
            )
        return built[key]

    return get
