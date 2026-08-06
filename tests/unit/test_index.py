"""Unit tests for the dotted-name index and root detection."""

from acquit.graph.index import build_index, detect_roots


def test_src_layout_dotted_names() -> None:
    files = ["src/app/__init__.py", "src/app/core.py", "src/app/util/io.py"]
    idx = build_index(files, ["src"])
    assert idx.by_dotted["app"] == ("src/app/__init__.py",)
    assert idx.by_dotted["app.core"] == ("src/app/core.py",)
    assert idx.by_dotted["app.util.io"] == ("src/app/util/io.py",)


def test_namespace_dir_without_init_still_indexed() -> None:
    idx = build_index(["ns/one.py", "ns/deeper/two.py"], [""])
    assert idx.by_dotted["ns.one"] == ("ns/one.py",)
    assert idx.by_dotted["ns.deeper.two"] == ("ns/deeper/two.py",)
    assert "ns" not in idx.by_dotted
    assert "ns.deeper" not in idx.by_dotted


def test_repo_root_as_root() -> None:
    idx = build_index(["pkg/__init__.py", "top.py"], [""])
    assert idx.by_dotted["pkg"] == ("pkg/__init__.py",)
    assert idx.by_dotted["top"] == ("top.py",)


def test_collision_across_roots_keeps_all_sorted() -> None:
    files = ["src/dup/m.py", "dup/m.py"]
    idx = build_index(files, ["src", ""])
    assert idx.by_dotted["dup.m"] == ("dup/m.py", "src/dup/m.py")


def test_file_under_two_roots_gets_both_dotted_names() -> None:
    idx = build_index(["src/a/b.py"], ["src", ""])
    assert idx.by_dotted["a.b"] == ("src/a/b.py",)
    assert idx.by_dotted["src.a.b"] == ("src/a/b.py",)
    assert idx.first_party_top_levels == frozenset({"a", "src"})


def test_first_party_top_levels() -> None:
    files = ["src/app/__init__.py", "tests/test_app.py", "conftest.py"]
    idx = build_index(files, ["src", ""])
    assert idx.first_party_top_levels == frozenset({"app", "src", "tests", "conftest"})


def test_root_level_init_yields_no_name() -> None:
    idx = build_index(["src/__init__.py"], ["src"])
    assert not idx.by_dotted
    assert not idx.first_party_top_levels


def test_non_python_files_ignored() -> None:
    idx = build_index(["pkg/data.json", "README.md", "pkg/mod.py"], [""])
    assert set(idx.by_dotted) == {"pkg.mod"}


def test_roots_normalized_and_deduped() -> None:
    idx = build_index(["src/a.py"], ["src/", "src", "./src", "."])
    assert idx.roots == ("src", "")


def test_files_outside_all_roots_are_absent() -> None:
    idx = build_index(["tools/gen.py", "src/a.py"], ["src"])
    assert set(idx.by_dotted) == {"a"}


def test_detect_roots_explicit_wins() -> None:
    assert detect_roots(["src/a.py"], ["lib/", "."]) == ("lib", "")


def test_detect_roots_src_only() -> None:
    assert detect_roots(["src/a.py", "src/pkg/b.py"]) == ("src",)


def test_detect_roots_mixed_layout() -> None:
    assert detect_roots(["src/a.py", "tests/test_a.py"]) == ("src", "")


def test_detect_roots_flat_layout() -> None:
    assert detect_roots(["pkg/a.py", "top.py"]) == ("",)


def test_detect_roots_no_files() -> None:
    assert detect_roots([]) == ("",)
