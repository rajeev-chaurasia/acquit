import pytest

from acquit.graph.model import EdgeKind
from acquit.pytestmap.conftree import (
    UNPARSEABLE_MARKER,
    ConftestFacts,
    conftest_scope_edges,
    inspect_conftest,
)

CONFTESTS = [
    "conftest.py",
    "pkg/conftest.py",
    "pkg/sub/conftest.py",
    "other/conftest.py",
]


@pytest.mark.parametrize(
    ("test_path", "expected_dsts"),
    [
        ("pkg/sub/test_leaf.py", ("conftest.py", "pkg/conftest.py", "pkg/sub/conftest.py")),
        (
            "pkg/sub/deeper/test_below.py",
            ("conftest.py", "pkg/conftest.py", "pkg/sub/conftest.py"),
        ),
        ("pkg/test_mid.py", ("conftest.py", "pkg/conftest.py")),
        ("test_root.py", ("conftest.py",)),
        ("elsewhere/test_far.py", ("conftest.py",)),
    ],
)
def test_scope_edges_follow_the_directory_chain(
    test_path: str, expected_dsts: tuple[str, ...]
) -> None:
    edges = conftest_scope_edges(test_path, CONFTESTS)
    assert tuple(edge.dst for edge in edges) == expected_dsts
    assert all(edge.src == test_path for edge in edges)
    assert all(edge.kind == EdgeKind.CONFTEST_SCOPE for edge in edges)


def test_scope_edges_order_is_deterministic() -> None:
    shuffled = ["pkg/sub/conftest.py", "conftest.py", "pkg/conftest.py"]
    edges = conftest_scope_edges("pkg/sub/test_x.py", shuffled)
    dsts = [edge.dst for edge in edges]
    assert dsts == ["conftest.py", "pkg/conftest.py", "pkg/sub/conftest.py"]
    assert dsts == sorted(dsts)


def test_sibling_directory_prefix_is_not_an_ancestor() -> None:
    assert conftest_scope_edges("tests_extra/test_x.py", ["tests/conftest.py"]) == ()


def test_no_edges_without_applicable_conftests() -> None:
    assert conftest_scope_edges("test_x.py", ["pkg/conftest.py"]) == ()


def test_duplicate_conftest_paths_produce_one_edge() -> None:
    edges = conftest_scope_edges("pkg/test_x.py", ["pkg/conftest.py", "pkg/conftest.py"])
    assert len(edges) == 1


@pytest.mark.parametrize(
    ("name", "source"),
    [
        (
            "pytest_collect_file",
            b"def pytest_collect_file(file_path, parent):\n    return None\n",
        ),
        (
            "pytest_ignore_collect",
            b"def pytest_ignore_collect(collection_path, config):\n    return False\n",
        ),
        (
            "pytest_pycollect_makemodule",
            b"def pytest_pycollect_makemodule(module_path, parent):\n    return None\n",
        ),
        ("collect_ignore", b'collect_ignore = ["setup.py"]\n'),
        ("collect_ignore_glob", b'collect_ignore_glob = ["*_skip.py"]\n'),
    ],
)
def test_each_collection_altering_name_is_detected(name: str, source: bytes) -> None:
    facts = inspect_conftest(source, "conftest.py")
    assert facts.collection_altering == (name,)


def test_benign_conftest_has_no_findings() -> None:
    source = b"""\
import pytest


def pytest_addoption(parser):
    parser.addoption("--slow", action="store_true")


@pytest.fixture
def client():
    return object()
"""
    facts = inspect_conftest(source, "tests/conftest.py")
    assert facts.path == "tests/conftest.py"
    assert facts.collection_altering == ()
    assert facts.pytest_plugins == ()


def test_multiple_hooks_are_sorted() -> None:
    source = b"""\
def pytest_collect_file(file_path, parent):
    return None


collect_ignore = ["setup.py"]
"""
    facts = inspect_conftest(source, "conftest.py")
    assert facts.collection_altering == ("collect_ignore", "pytest_collect_file")


def test_annotated_and_tuple_assignments_count() -> None:
    annotated = inspect_conftest(b"collect_ignore: list[str] = []\n", "conftest.py")
    assert annotated.collection_altering == ("collect_ignore",)
    unpacked = inspect_conftest(b'collect_ignore_glob, other = ["*_x.py"], 1\n', "conftest.py")
    assert unpacked.collection_altering == ("collect_ignore_glob",)


def test_nested_definition_still_counts() -> None:
    source = b"""\
import sys

if sys.platform == "win32":
    def pytest_ignore_collect(collection_path, config):
        return True
"""
    facts = inspect_conftest(source, "conftest.py")
    assert facts.collection_altering == ("pytest_ignore_collect",)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b'pytest_plugins = "celery.contrib.pytest"\n', ("celery.contrib.pytest",)),
        (b'pytest_plugins = ["plug_a", "plug_b"]\n', ("plug_a", "plug_b")),
        (b'pytest_plugins = ("plug_a",)\n', ("plug_a",)),
        (b'pytest_plugins: list[str] = ["plug_a"]\n', ("plug_a",)),
        (b"pytest_plugins = get_plugins()\n", ()),
        (b"other_name = ['plug_a']\n", ()),
    ],
)
def test_pytest_plugins_literal_extraction(source: bytes, expected: tuple[str, ...]) -> None:
    facts = inspect_conftest(source, "conftest.py")
    assert facts.pytest_plugins == expected


@pytest.mark.parametrize(
    "source",
    [
        b"def broken(:\n    pass\n",
        b"\x00",
        "collect = 1".encode("utf-16"),
    ],
)
def test_unparseable_conftest_is_marked(source: bytes) -> None:
    facts = inspect_conftest(source, "pkg/conftest.py")
    assert facts == ConftestFacts(
        path="pkg/conftest.py",
        collection_altering=(UNPARSEABLE_MARKER,),
        pytest_plugins=(),
    )
