"""Helpers for the fail-closed delivery reproductions.

Everything here drives the real artifacts: throwaway git repositories, the real
acquit CLI, real pytest subprocesses that load the installed plugin, and the
bash body lifted out of action.yml. Nothing is simulated except the two
commands the composite action shells out to.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import chdir
from pathlib import Path
from typing import Any

import pytest

from acquit.cli import main
from acquit.constants import ENV_SELECTION_FILE, SELECTION_SCHEMA
from acquit.vcs import working_tree_fingerprint

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTION_YML = PROJECT_ROOT / "action.yml"

GITIGNORE = ".acquit/\nacquit-*.json\n__pycache__/\n*.pyc\n.pytest_cache/\n"

ALPHA_TEST = "import alpha\n\n\ndef test_alpha():\n    assert alpha.ALPHA\n"
BETA_TEST = "import beta\n\n\ndef test_beta():\n    assert beta.BETA\n"


def require_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not available")


def bash_for(workspace: Path) -> str:
    """An absolute bash that can read workspace, or skip.

    Resolving by name is not enough on Windows, where the first bash on PATH
    is often the WSL one, which cannot see a drive-letter path at all.
    """
    candidates = [shutil.which("bash")]
    git_exe = shutil.which("git")
    if git_exe is not None:
        root = Path(git_exe).resolve().parents[1]
        candidates += [
            str(root / "bin" / "bash.exe"),
            str(root.parent / "usr" / "bin" / "bash.exe"),
        ]
    for candidate in candidates:
        if candidate is None or not Path(candidate).exists():
            continue
        probe = subprocess.run(
            [candidate, "-c", f'test -d "{workspace.as_posix()}"'], capture_output=True, check=False
        )
        if probe.returncode == 0:
            return candidate
    pytest.skip("no bash on this machine can read the test workspace")


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return completed.stdout.strip()


def new_repo(path: Path) -> Path:
    """Initialise a throwaway repository with deterministic git settings."""
    require_git()
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Acquit Adversary")
    git(path, "config", "user.email", "adversary@acquit.invalid")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "core.autocrlf", "false")
    write(path, {".gitignore": GITIGNORE})
    return path


def write(repo: Path, files: Mapping[str, str]) -> None:
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def two_module_repo(path: Path) -> Path:
    """alpha.py and beta.py, one test file importing each."""
    repo = new_repo(path)
    write(
        repo,
        {
            "alpha.py": "ALPHA = 1\n",
            "beta.py": "BETA = 1\n",
            "tests/test_alpha.py": ALPHA_TEST,
            "tests/test_beta.py": BETA_TEST,
        },
    )
    return repo


def select(repo: Path, out: Path, base: str, head: str | None = None) -> int:
    """Run the real CLI select command with its documents under out."""
    out.mkdir(parents=True, exist_ok=True)
    args = [
        "select",
        "--base",
        base,
        "--report",
        str(out / "report.json"),
        "--selection",
        str(out / "selection.json"),
        "--witnesses",
        str(out / "witnesses.json"),
    ]
    if head is not None:
        args += ["--head", head]
    with chdir(repo):
        return main(args)


def replay(repo: Path, out: Path) -> int:
    with chdir(repo):
        return main(
            [
                "replay",
                str(out / "report.json"),
                "--witnesses",
                str(out / "witnesses.json"),
                "--selection",
                str(out / "selection.json"),
            ]
        )


def read_json(path: Path) -> dict[str, Any]:
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def write_json(path: Path, document: Any) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def selection_doc(
    skip: Sequence[str],
    mode: str = "selective",
    graph_hash: Any = "0" * 64,
    tree: Path | None = None,
) -> Any:
    """A hand-built selection-v2 document, optionally bound to a real tree."""
    fingerprint = working_tree_fingerprint(tree) if tree is not None else None
    return {
        "schema": SELECTION_SCHEMA,
        "mode": mode,
        "graph_hash": graph_hash,
        "tree": {"head_sha": None, "fingerprint": fingerprint},
        "skip": [{"path": path, "witness": f"w-{index:06d}"} for index, path in enumerate(skip, 1)],
    }


def skip_paths(document: Mapping[str, Any]) -> list[str]:
    """The skipped test paths of a selection document, in document order."""
    return [entry["path"] for entry in document["skip"]]


def run_pytest(
    cwd: Path, selection: Path | str | None, *args: str, env_extra: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run real pytest in a subprocess so the installed acquit plugin applies."""
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop(ENV_SELECTION_FILE, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(cwd)
    if selection is not None:
        env[ENV_SELECTION_FILE] = str(selection)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def outcome(result: subprocess.CompletedProcess[str]) -> str:
    """The pytest summary line, for readable assertion failures."""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else result.stderr.strip()


def deselected_count(result: subprocess.CompletedProcess[str]) -> int:
    match = re.search(r"(\d+) deselected", result.stdout)
    return int(match.group(1)) if match else 0


def action_select_script() -> str:
    """The bash body of the select step, lifted verbatim out of action.yml."""
    text = ACTION_YML.read_text(encoding="utf-8")
    marker = "run: |\n"
    start = text.index(marker) + len(marker)
    body: list[str] = []
    for line in text[start:].splitlines():
        if line.strip() and not line.startswith(" " * 8):
            break
        body.append(line[8:])
    # ${{ }} expressions are substituted by the Actions runner, not by bash.
    return re.sub(r"\$\{\{.*?\}\}", "0.0.1", "\n".join(body) + "\n")
