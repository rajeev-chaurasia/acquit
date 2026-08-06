"""Shared fixtures: throwaway git repositories driven through real commits."""

import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest


def run_git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return completed.stdout.strip()


def init_repo(path: Path) -> Path:
    run_git(path, "init", "-q", "-b", "main")
    run_git(path, "config", "user.name", "Acquit Tests")
    run_git(path, "config", "user.email", "tests@acquit.invalid")
    run_git(path, "config", "commit.gpgsign", "false")
    run_git(path, "config", "core.autocrlf", "false")
    return path


def write_files(repo: Path, files: Mapping[str, str]) -> None:
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def commit_all(repo: Path, message: str) -> str:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


def module_test_source(module: str) -> str:
    return f"import {module}\n\n\ndef test_{module}():\n    assert {module}\n"


class RepoBuilder:
    """Small imperative builder for one throwaway git repository."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, files: Mapping[str, str]) -> None:
        write_files(self.path, files)

    def remove(self, name: str) -> None:
        (self.path / name).unlink()

    def commit(self, message: str) -> str:
        return commit_all(self.path, message)


@dataclass(frozen=True)
class ScenarioRepo:
    """A prebuilt commit chain covering the end-to-end selection scenarios."""

    path: Path
    base: str
    alpha_change: str
    delta_removal: str
    conftest_change: str
    manifest_change: str


_GITIGNORE = ".acquit/\nacquit-*.json\n__pycache__/\n*.pyc\n.pytest_cache/\n"


@pytest.fixture
def repo_builder(tmp_path: Path) -> RepoBuilder:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    return RepoBuilder(init_repo(tmp_path))


@pytest.fixture(scope="session")
def scenario_repo(tmp_path_factory: pytest.TempPathFactory) -> ScenarioRepo:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    repo = init_repo(tmp_path_factory.mktemp("scenario"))
    write_files(
        repo,
        {
            ".gitignore": _GITIGNORE,
            "alpha.py": "ALPHA = 1\n",
            "beta.py": "BETA = 1\n",
            "delta.py": "import delta_extra\n\nDELTA = delta_extra.EXTRA\n",
            "delta_extra.py": "EXTRA = 3\n",
            "tests/test_alpha.py": module_test_source("alpha"),
            "tests/test_beta.py": module_test_source("beta"),
            "tests/test_delta.py": module_test_source("delta"),
            "tests/pkg/conftest.py": "PKG_MARK = 1\n",
            "tests/pkg/test_pkg.py": "def test_pkg():\n    assert True\n",
        },
    )
    base = commit_all(repo, "base")
    write_files(repo, {"alpha.py": "ALPHA = 2\n"})
    alpha_change = commit_all(repo, "change alpha")
    (repo / "delta_extra.py").unlink()
    write_files(repo, {"delta.py": "DELTA = 3\n"})
    delta_removal = commit_all(repo, "inline delta_extra")
    write_files(repo, {"tests/pkg/conftest.py": "PKG_MARK = 2\n"})
    conftest_change = commit_all(repo, "touch pkg conftest")
    write_files(repo, {"pyproject.toml": '[project]\nname = "scenario"\nversion = "0.0.0"\n'})
    manifest_change = commit_all(repo, "add manifest")
    return ScenarioRepo(
        path=repo,
        base=base,
        alpha_change=alpha_change,
        delta_removal=delta_removal,
        conftest_change=conftest_change,
        manifest_change=manifest_change,
    )
