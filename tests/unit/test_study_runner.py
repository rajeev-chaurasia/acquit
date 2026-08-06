"""Pure pieces of the study runner: suite venv install command assembly."""

from pathlib import Path

from acquit.study.runner import install_args

PYTHON = Path("wt") / ".study-venv" / "bin" / "python"
BASE = ["uv", "pip", "install", "--python", str(PYTHON), "-e", ".", "pytest"]


def test_install_args_minimal_recipe() -> None:
    assert install_args(PYTHON, (), None) == BASE


def test_install_args_appends_suite_deps_in_manifest_order() -> None:
    args = install_args(PYTHON, ("pytest<9", "attrs"), None)
    assert args == [*BASE, "pytest<9", "attrs"]


def test_install_args_puts_constraints_last() -> None:
    constraints = Path("study") / "constraints" / "flask.txt"
    args = install_args(PYTHON, ("trio",), constraints)
    assert args == [*BASE, "trio", "--constraints", str(constraints)]


def test_install_args_without_suite_deps_still_pins() -> None:
    constraints = Path("c.txt")
    args = install_args(PYTHON, (), constraints)
    assert args == [*BASE, "--constraints", str(constraints)]
