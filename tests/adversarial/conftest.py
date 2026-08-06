"""Fixtures for adversarial soundness reproductions.

Each reproduction drives the real pipeline over a throwaway git repository and,
where a premise rests on pytest runtime semantics, re-establishes ground truth
by running real pytest in a subprocess against the working tree.
"""

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from acquit.pipeline import SelectResult

_GITIGNORE = ".acquit/\n__pycache__/\n*.pyc\n.pytest_cache/\n"


def run_git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return completed.stdout.strip()


def buckets(result: SelectResult) -> tuple[set[str], set[str], set[str]]:
    return (
        {entry.path for entry in result.decision.selected},
        {entry.path for entry in result.decision.skipped},
        {entry.path for entry in result.decision.always_run},
    )


class AdvRepo:
    """One throwaway repository plus a real pytest runner for ground truth."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, files: Mapping[str, str]) -> None:
        for name, content in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")

    def commit(self, message: str) -> str:
        run_git(self.path, "add", "-A")
        run_git(self.path, "commit", "-q", "-m", message)
        return run_git(self.path, "rev-parse", "HEAD")

    def run_pytest(self) -> subprocess.CompletedProcess[str]:
        """Run real pytest against the current working tree state."""
        env = dict(os.environ)
        env.pop("ACQUIT_SELECTION_FILE", None)
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=self.path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )


@pytest.fixture
def adv_repo(tmp_path: Path) -> AdvRepo:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.name", "Acquit Adversary")
    run_git(tmp_path, "config", "user.email", "adversary@acquit.invalid")
    run_git(tmp_path, "config", "commit.gpgsign", "false")
    run_git(tmp_path, "config", "core.autocrlf", "false")
    repo = AdvRepo(tmp_path)
    repo.write({".gitignore": _GITIGNORE})
    return repo
