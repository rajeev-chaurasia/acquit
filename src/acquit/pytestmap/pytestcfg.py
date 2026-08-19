"""Static view of pytest configuration.

Mirrors pytest's own precedence without importing pytest: pytest.ini beats
pyproject.toml [tool.pytest.ini_options] beats tox.ini [pytest] beats
setup.cfg [tool:pytest]. pytest.ini wins by existing, matching pytest;
every other file only counts when it contains its pytest section.
"""

import configparser
import shlex
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Final

from acquit.errors import AcquitError

DEFAULT_PYTHON_FILES: Final = ("test_*.py", "*_test.py")
DEFAULT_NORECURSEDIRS: Final = (
    "*.egg",
    ".*",
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
)

_LIST_KEYS: Final = ("python_files", "testpaths", "norecursedirs", "pythonpath")

# (filename, ini section); pyproject.toml is handled separately as toml.
_CANDIDATES: Final = (
    ("pytest.ini", "pytest"),
    ("pyproject.toml", ""),
    ("tox.ini", "pytest"),
    ("setup.cfg", "tool:pytest"),
)


@dataclass(frozen=True, slots=True)
class PytestConfig:
    """Everything acquit needs to know about how pytest is configured."""

    source: str | None
    python_files: tuple[str, ...]
    testpaths: tuple[str, ...]
    norecursedirs: tuple[str, ...]
    addopts: tuple[str, ...]
    pythonpath: tuple[str, ...]
    doctest_modules: bool
    extra_plugins: tuple[str, ...]
    # Repo-relative directory pytest treats as its root. An empty testpaths
    # setting collects below this directory, not below the Git root.
    rootdir: str = ""


def _as_word_list(value: object, key: str, source: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(value.split())
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise AcquitError(f"{source}: {key} must be a string or a list of strings")


def _as_addopts(value: object, source: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            return tuple(shlex.split(value))
        except ValueError as error:
            raise AcquitError(f"{source}: addopts is not shell-splittable: {error}") from error
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise AcquitError(f"{source}: addopts must be a string or a list of strings")


def _extra_plugins(addopts: Sequence[str]) -> tuple[str, ...]:
    plugins: list[str] = []
    expecting_value = False
    for token in addopts:
        if expecting_value:
            expecting_value = False
            if not token.startswith("no:"):
                plugins.append(token)
        elif token == "-p":
            expecting_value = True
        elif token.startswith("-p") and not token.startswith("-pno:"):
            plugins.append(token[2:])
    return tuple(plugins)


def _rebase_paths(
    values: tuple[str, ...], config_dir: Path, repo_root: Path, source: str
) -> tuple[str, ...]:
    rebased: list[str] = []
    resolved_repo = repo_root.resolve()
    for value in values:
        try:
            relative = (config_dir / value).resolve().relative_to(resolved_repo).as_posix()
        except (OSError, ValueError) as error:
            raise AcquitError(
                f"{source}: configured path {value!r} resolves outside the repository"
            ) from error
        if relative == ".":
            relative = ""
        if relative not in rebased:
            rebased.append(relative)
    return tuple(rebased)


def _build_config(
    source: str | None,
    raw: Mapping[str, object],
    *,
    repo_root: Path | None = None,
    config_dir: Path | None = None,
) -> PytestConfig:
    lists = {key: _as_word_list(raw[key], key, source or "?") for key in _LIST_KEYS if key in raw}
    addopts = _as_addopts(raw["addopts"], source or "?") if "addopts" in raw else ()
    rootdir = ""
    if source is not None and repo_root is not None and config_dir is not None:
        rootdir = config_dir.resolve().relative_to(repo_root.resolve()).as_posix()
        if rootdir == ".":
            rootdir = ""
        for key in ("testpaths", "pythonpath"):
            if key in lists:
                lists[key] = _rebase_paths(lists[key], config_dir, repo_root, source)
    return PytestConfig(
        source=source,
        python_files=lists.get("python_files", DEFAULT_PYTHON_FILES),
        testpaths=lists.get("testpaths", ()),
        norecursedirs=lists.get("norecursedirs", DEFAULT_NORECURSEDIRS),
        addopts=addopts,
        pythonpath=lists.get("pythonpath", ()),
        doctest_modules="--doctest-modules" in addopts,
        extra_plugins=_extra_plugins(addopts),
        rootdir=rootdir,
    )


def _read_ini_section(path: Path, section: str) -> Mapping[str, object] | None:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(encoding="utf-8"), source=path.name)
    except (configparser.Error, UnicodeDecodeError) as error:
        raise AcquitError(f"{path.name}: unreadable ini file: {error}") from error
    if not parser.has_section(section):
        return None
    return dict(parser[section])


def _read_toml_section(path: Path) -> Mapping[str, object] | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise AcquitError(f"{path.name}: unreadable toml file: {error}") from error
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return None
    pytest_table = tool.get("pytest")
    if not isinstance(pytest_table, dict):
        return None
    ini_options = pytest_table.get("ini_options")
    if not isinstance(ini_options, dict):
        return None
    return {str(key): value for key, value in ini_options.items()}


def _config_in_directory(directory: Path, repo_root: Path) -> PytestConfig | None:
    for filename, section in _CANDIDATES:
        path = directory / filename
        if not path.is_file():
            continue
        if filename == "pyproject.toml":
            raw = _read_toml_section(path)
        else:
            raw = _read_ini_section(path, section)
        if raw is None and filename == "pytest.ini":
            # pytest treats a pytest.ini as authoritative even without a
            # [pytest] section; other candidates need their section present.
            raw = {}
        if raw is not None:
            source = path.resolve().relative_to(repo_root.resolve()).as_posix()
            return _build_config(source, raw, repo_root=repo_root, config_dir=directory)
    return None


def _ancestors(start: Path, stop: Path) -> tuple[Path, ...]:
    current = start.resolve()
    resolved_stop = stop.resolve()
    if not current.is_relative_to(resolved_stop):
        raise AcquitError(f"pytest invocation directory {start} is outside repository {stop}")
    out: list[Path] = []
    while True:
        out.append(current)
        if current == resolved_stop:
            return tuple(out)
        current = current.parent


def _nested_configs(repo_root: Path, searched: set[Path]) -> tuple[PytestConfig, ...]:
    directories: set[Path] = set()
    candidate_names = {filename for filename, _ in _CANDIDATES}
    for directory, dirnames, filenames in repo_root.walk():
        # Avoid both false configs and expensive traversal in environments,
        # dependency trees, build output, and hidden metadata such as .git.
        dirnames[:] = [
            name
            for name in dirnames
            if not any(fnmatchcase(name, pattern) for pattern in DEFAULT_NORECURSEDIRS)
        ]
        if directory.resolve() not in searched and candidate_names.intersection(filenames):
            directories.add(directory)
    configs = [
        config
        for directory in sorted(directories, key=lambda item: item.as_posix())
        if (config := _config_in_directory(directory, repo_root)) is not None
    ]
    return tuple(configs)


def load_pytest_config(repo_root: Path, invocation_dir: Path | None = None) -> PytestConfig:
    """Locate and statically parse the pytest config for one invocation.

    This is the only function in the pytest mapping layer that touches the
    filesystem. Pytest searches from the invocation directory upward. When a
    Git-root invocation has no config above it, Acquit also recognizes one
    unambiguous nested suite config so monorepo ``backend/`` layouts work from
    either directory. Multiple nested suite configs fail closed rather than
    merging incompatible collection rules.
    """
    start = repo_root if invocation_dir is None else invocation_dir
    ancestors = _ancestors(start, repo_root)
    for directory in ancestors:
        config = _config_in_directory(directory, repo_root)
        if config is not None:
            return config

    nested = _nested_configs(repo_root, {path.resolve() for path in ancestors})
    if len(nested) == 1:
        return nested[0]
    if len(nested) > 1:
        sources = ", ".join(config.source or "?" for config in nested)
        raise AcquitError(
            "multiple nested pytest configurations found; invoke acquit from one suite "
            f"directory or add a repository-level pytest config: {sources}"
        )
    return _build_config(None, {})
