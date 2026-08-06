from pathlib import Path

import pytest

from acquit.errors import AcquitError
from acquit.pytestmap.pytestcfg import (
    DEFAULT_NORECURSEDIRS,
    DEFAULT_PYTHON_FILES,
    load_pytest_config,
)

PYTEST_INI = """\
[pytest]
python_files = alpha_*.py
"""

PYPROJECT_TOML = """\
[tool.pytest.ini_options]
python_files = ["beta_*.py"]
"""

TOX_INI = """\
[tox]
envlist = py312

[pytest]
python_files = gamma_*.py
"""

SETUP_CFG = """\
[metadata]
name = demo

[tool:pytest]
python_files = delta_*.py
"""

PYTEST_INI_NO_SECTION = """\
[flake8]
max-line-length = 100
"""

PYPROJECT_NO_SECTION = """\
[tool.ruff]
line-length = 100
"""

TOX_INI_NO_SECTION = """\
[tox]
envlist = py312
"""

ALL_FOUR = {
    "pytest.ini": PYTEST_INI,
    "pyproject.toml": PYPROJECT_TOML,
    "tox.ini": TOX_INI,
    "setup.cfg": SETUP_CFG,
}


def write_all(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")


@pytest.mark.parametrize(
    ("files", "expected_source", "expected_python_files"),
    [
        (ALL_FOUR, "pytest.ini", ("alpha_*.py",)),
        (
            {"pyproject.toml": PYPROJECT_TOML, "tox.ini": TOX_INI, "setup.cfg": SETUP_CFG},
            "pyproject.toml",
            ("beta_*.py",),
        ),
        ({"tox.ini": TOX_INI, "setup.cfg": SETUP_CFG}, "tox.ini", ("gamma_*.py",)),
        ({"setup.cfg": SETUP_CFG}, "setup.cfg", ("delta_*.py",)),
        (
            # pytest.ini wins by existing, even without a [pytest] section.
            {"pytest.ini": PYTEST_INI_NO_SECTION, "pyproject.toml": PYPROJECT_TOML},
            "pytest.ini",
            DEFAULT_PYTHON_FILES,
        ),
        (
            {"pyproject.toml": PYPROJECT_NO_SECTION, "tox.ini": TOX_INI},
            "tox.ini",
            ("gamma_*.py",),
        ),
        (
            {"tox.ini": TOX_INI_NO_SECTION, "setup.cfg": SETUP_CFG},
            "setup.cfg",
            ("delta_*.py",),
        ),
    ],
)
def test_precedence(
    tmp_path: Path,
    files: dict[str, str],
    expected_source: str,
    expected_python_files: tuple[str, ...],
) -> None:
    write_all(tmp_path, files)
    cfg = load_pytest_config(tmp_path)
    assert cfg.source == expected_source
    assert cfg.python_files == expected_python_files


def test_defaults_when_nothing_found(tmp_path: Path) -> None:
    cfg = load_pytest_config(tmp_path)
    assert cfg.source is None
    assert cfg.python_files == DEFAULT_PYTHON_FILES
    assert cfg.testpaths == ()
    assert cfg.norecursedirs == DEFAULT_NORECURSEDIRS
    assert cfg.addopts == ()
    assert cfg.pythonpath == ()
    assert cfg.doctest_modules is False
    assert cfg.extra_plugins == ()


def test_unset_keys_keep_defaults(tmp_path: Path) -> None:
    write_all(tmp_path, {"pytest.ini": PYTEST_INI})
    cfg = load_pytest_config(tmp_path)
    assert cfg.python_files == ("alpha_*.py",)
    assert cfg.norecursedirs == DEFAULT_NORECURSEDIRS
    assert cfg.testpaths == ()


def test_ini_values_split_on_whitespace(tmp_path: Path) -> None:
    content = """\
[pytest]
python_files =
    check_*.py
    test_*.py
testpaths = tests integration
norecursedirs = .tox build
pythonpath = src lib
"""
    write_all(tmp_path, {"pytest.ini": content})
    cfg = load_pytest_config(tmp_path)
    assert cfg.python_files == ("check_*.py", "test_*.py")
    assert cfg.testpaths == ("tests", "integration")
    assert cfg.norecursedirs == (".tox", "build")
    assert cfg.pythonpath == ("src", "lib")


def test_toml_array_values(tmp_path: Path) -> None:
    content = """\
[tool.pytest.ini_options]
python_files = ["check_*.py", "test_*.py"]
testpaths = ["tests", "integration"]
norecursedirs = [".tox", "build"]
pythonpath = ["src"]
addopts = ["-q", "--doctest-modules", "-p", "myplugin"]
"""
    write_all(tmp_path, {"pyproject.toml": content})
    cfg = load_pytest_config(tmp_path)
    assert cfg.python_files == ("check_*.py", "test_*.py")
    assert cfg.testpaths == ("tests", "integration")
    assert cfg.norecursedirs == (".tox", "build")
    assert cfg.pythonpath == ("src",)
    assert cfg.addopts == ("-q", "--doctest-modules", "-p", "myplugin")
    assert cfg.doctest_modules is True
    assert cfg.extra_plugins == ("myplugin",)


def test_toml_string_values(tmp_path: Path) -> None:
    content = """\
[tool.pytest.ini_options]
python_files = "check_*.py test_*.py"
testpaths = "tests integration"
addopts = "-q -k 'not slow'"
"""
    write_all(tmp_path, {"pyproject.toml": content})
    cfg = load_pytest_config(tmp_path)
    assert cfg.python_files == ("check_*.py", "test_*.py")
    assert cfg.testpaths == ("tests", "integration")
    assert cfg.addopts == ("-q", "-k", "not slow")


def test_ini_addopts_is_shlex_split(tmp_path: Path) -> None:
    content = """\
[pytest]
addopts = -q -k "not slow" --doctest-modules
"""
    write_all(tmp_path, {"pytest.ini": content})
    cfg = load_pytest_config(tmp_path)
    assert cfg.addopts == ("-q", "-k", "not slow", "--doctest-modules")
    assert cfg.doctest_modules is True


@pytest.mark.parametrize(
    ("addopts", "expected"),
    [
        ("-p myplugin", ("myplugin",)),
        ("-pmyplugin", ("myplugin",)),
        ("-p no:cacheprovider", ()),
        ("-pno:cacheprovider", ()),
        ("-q -p one -p no:two -pthree", ("one", "three")),
        ("--doctest-modules", ()),
        ("", ()),
    ],
)
def test_extra_plugins_extraction(tmp_path: Path, addopts: str, expected: tuple[str, ...]) -> None:
    write_all(tmp_path, {"pytest.ini": f"[pytest]\naddopts = {addopts}\n"})
    cfg = load_pytest_config(tmp_path)
    assert cfg.extra_plugins == expected


def test_doctest_modules_absent_by_default(tmp_path: Path) -> None:
    write_all(tmp_path, {"pytest.ini": "[pytest]\naddopts = -q\n"})
    assert load_pytest_config(tmp_path).doctest_modules is False


def test_wrong_toml_type_raises(tmp_path: Path) -> None:
    content = "[tool.pytest.ini_options]\npython_files = 5\n"
    write_all(tmp_path, {"pyproject.toml": content})
    with pytest.raises(AcquitError):
        load_pytest_config(tmp_path)


def test_broken_toml_raises(tmp_path: Path) -> None:
    write_all(tmp_path, {"pyproject.toml": "[tool.pytest\n"})
    with pytest.raises(AcquitError):
        load_pytest_config(tmp_path)


def test_broken_ini_raises(tmp_path: Path) -> None:
    write_all(tmp_path, {"pytest.ini": "python_files = orphan value\n"})
    with pytest.raises(AcquitError):
        load_pytest_config(tmp_path)
