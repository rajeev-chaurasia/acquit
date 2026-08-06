"""Pure static test discovery over a repo file listing.

Reimplements just enough of pytest's collection rules (python_files matching,
testpaths restriction, norecursedirs pruning) to predict which files pytest
would collect, without ever running it.
"""

from collections.abc import Sequence
from fnmatch import fnmatchcase
from typing import Final

from acquit.graph.model import NodeKind
from acquit.pytestmap.pytestcfg import PytestConfig

_CONFTEST: Final = "conftest.py"

_CONFIG_BASENAMES: Final = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pytest.ini",
        "tox.ini",
        "uv.lock",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "sitecustomize.py",
        "usercustomize.py",
    }
)
_CONFIG_BASENAME_PATTERNS: Final = ("requirements*.txt", "constraints*.txt", "*.pth")
_WORKFLOWS_PREFIX: Final = ".github/workflows/"


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _matches_any(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def _within_testpaths(path: str, testpaths: Sequence[str]) -> bool:
    for testpath in testpaths:
        prefix = testpath.removeprefix("./").rstrip("/")
        if not prefix or path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _under_norecursedir(path: str, patterns: Sequence[str]) -> bool:
    return any(_matches_any(part, patterns) for part in path.split("/")[:-1])


def discover_test_files(files: Sequence[str], cfg: PytestConfig) -> tuple[str, ...]:
    """Predict which of the given repo-relative files pytest would collect as tests."""
    selected: set[str] = set()
    for path in files:
        name = _basename(path)
        if name == _CONFTEST:
            continue
        if not _matches_any(name, cfg.python_files):
            continue
        if cfg.testpaths and not _within_testpaths(path, cfg.testpaths):
            continue
        if _under_norecursedir(path, cfg.norecursedirs):
            continue
        selected.add(path)
    return tuple(sorted(selected))


def _is_config(path: str, name: str, cfg: PytestConfig) -> bool:
    return (
        path == cfg.source
        or name in _CONFIG_BASENAMES
        or _matches_any(name, _CONFIG_BASENAME_PATTERNS)
        or path.startswith(_WORKFLOWS_PREFIX)
    )


def classify_file(path: str, cfg: PytestConfig, test_files: frozenset[str]) -> NodeKind:
    """Map a repo-relative path to its node kind. Unknown files are resources."""
    if path in test_files:
        return NodeKind.TEST
    name = _basename(path)
    if name == _CONFTEST:
        return NodeKind.CONFTEST
    if path.endswith(".pyi"):
        return NodeKind.STUB
    if _is_config(path, name, cfg):
        return NodeKind.CONFIG
    if path.endswith(".py"):
        return NodeKind.MODULE
    return NodeKind.RESOURCE
