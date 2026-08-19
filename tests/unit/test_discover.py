import pytest

from acquit.graph.model import NodeKind
from acquit.pytestmap.discover import classify_file, discover_test_files
from acquit.pytestmap.pytestcfg import (
    DEFAULT_NORECURSEDIRS,
    DEFAULT_PYTHON_FILES,
    PytestConfig,
)


def make_cfg(
    *,
    source: str | None = None,
    python_files: tuple[str, ...] = DEFAULT_PYTHON_FILES,
    testpaths: tuple[str, ...] = (),
    norecursedirs: tuple[str, ...] = DEFAULT_NORECURSEDIRS,
    rootdir: str = "",
) -> PytestConfig:
    return PytestConfig(
        source=source,
        python_files=python_files,
        testpaths=testpaths,
        norecursedirs=norecursedirs,
        addopts=(),
        pythonpath=(),
        doctest_modules=False,
        extra_plugins=(),
        rootdir=rootdir,
    )


def test_basename_matching_with_default_patterns() -> None:
    files = [
        "tests/test_app.py",
        "tests/app_test.py",
        "tests/helper.py",
        "src/pkg/module.py",
        "tests/test_data.json",
    ]
    assert discover_test_files(files, make_cfg()) == ("tests/app_test.py", "tests/test_app.py")


def test_custom_python_files_patterns() -> None:
    cfg = make_cfg(python_files=("check_*.py",))
    files = ["tests/check_math.py", "tests/test_app.py"]
    assert discover_test_files(files, cfg) == ("tests/check_math.py",)


def test_testpaths_restriction_is_a_path_prefix() -> None:
    cfg = make_cfg(testpaths=("tests",))
    files = [
        "tests/test_a.py",
        "tests/sub/test_b.py",
        "src/test_c.py",
        "tests_extra/test_d.py",
        "test_root.py",
    ]
    assert discover_test_files(files, cfg) == ("tests/sub/test_b.py", "tests/test_a.py")


def test_multiple_testpaths() -> None:
    cfg = make_cfg(testpaths=("tests", "integration/suite"))
    files = [
        "tests/test_a.py",
        "integration/suite/test_b.py",
        "integration/other/test_c.py",
    ]
    expected = ("integration/suite/test_b.py", "tests/test_a.py")
    assert discover_test_files(files, cfg) == expected


def test_nested_rootdir_bounds_default_collection() -> None:
    cfg = make_cfg(rootdir="backend")
    files = ["backend/tests/test_api.py", "frontend/tests/test_ui.py", "test_root.py"]
    assert discover_test_files(files, cfg) == ("backend/tests/test_api.py",)


def test_explicit_testpaths_can_reach_outside_nested_rootdir() -> None:
    cfg = make_cfg(rootdir="backend", testpaths=("shared/tests",))
    files = ["backend/tests/test_api.py", "shared/tests/test_contract.py"]
    assert discover_test_files(files, cfg) == ("shared/tests/test_contract.py",)


@pytest.mark.parametrize(
    "path",
    [
        "build/test_built.py",
        "pkg/node_modules/test_dep.py",
        ".hidden/test_hidden.py",
        "deep/dist/nested/more/test_deep.py",
        "demo.egg/test_egg.py",
        "venv/lib/test_env.py",
    ],
)
def test_norecursedirs_excludes_at_any_depth(path: str) -> None:
    assert discover_test_files([path], make_cfg()) == ()


def test_norecursedirs_applies_to_directories_not_basenames() -> None:
    assert discover_test_files(["tests/build_test.py"], make_cfg()) == ("tests/build_test.py",)


def test_conftest_is_never_a_test_file() -> None:
    cfg = make_cfg(python_files=("*.py",))
    files = ["tests/conftest.py", "conftest.py", "tests/test_a.py"]
    assert discover_test_files(files, cfg) == ("tests/test_a.py",)


def test_output_is_sorted_and_deduplicated() -> None:
    files = ["b/test_b.py", "a/test_a.py", "b/test_b.py"]
    assert discover_test_files(files, make_cfg()) == ("a/test_a.py", "b/test_b.py")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_app.py", NodeKind.TEST),
        ("tests/conftest.py", NodeKind.CONFTEST),
        ("conftest.py", NodeKind.CONFTEST),
        ("src/pkg/api.pyi", NodeKind.STUB),
        ("src/pkg/api.py", NodeKind.MODULE),
        ("scripts/check_style.py", NodeKind.MODULE),
        ("pyproject.toml", NodeKind.CONFIG),
        ("setup.py", NodeKind.CONFIG),
        ("setup.cfg", NodeKind.CONFIG),
        ("pytest.ini", NodeKind.CONFIG),
        ("tox.ini", NodeKind.CONFIG),
        ("requirements.txt", NodeKind.CONFIG),
        ("requirements-dev.txt", NodeKind.CONFIG),
        ("constraints.txt", NodeKind.CONFIG),
        ("constraints-prod.txt", NodeKind.CONFIG),
        ("uv.lock", NodeKind.CONFIG),
        ("poetry.lock", NodeKind.CONFIG),
        ("Pipfile", NodeKind.CONFIG),
        ("Pipfile.lock", NodeKind.CONFIG),
        (".github/workflows/ci.yml", NodeKind.CONFIG),
        (".github/workflows/release.yaml", NodeKind.CONFIG),
        (".github/dependabot.yml", NodeKind.RESOURCE),
        ("src/editable.pth", NodeKind.CONFIG),
        ("sitecustomize.py", NodeKind.CONFIG),
        ("usercustomize.py", NodeKind.CONFIG),
        ("data/fixtures.json", NodeKind.RESOURCE),
        ("README.md", NodeKind.RESOURCE),
        ("requirements/base.txt", NodeKind.RESOURCE),
    ],
)
def test_classify_file(path: str, expected: NodeKind) -> None:
    cfg = make_cfg(source="pyproject.toml")
    test_files = frozenset({"tests/test_app.py"})
    assert classify_file(path, cfg, test_files) == expected


def test_classify_prefers_test_membership_over_module() -> None:
    test_files = frozenset({"src/pkg/checks.py"})
    assert classify_file("src/pkg/checks.py", make_cfg(), test_files) == NodeKind.TEST


def test_classify_winning_config_source_is_config() -> None:
    cfg = make_cfg(source="pyproject.toml")
    assert classify_file("pyproject.toml", cfg, frozenset()) == NodeKind.CONFIG
