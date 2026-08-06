"""Table-driven corpus for resolve_import.

Each case is pure data: a repository file listing, roots (None means
auto-detect), an importer, one import statement, and the exact expected
Resolution. These cases are the executable specification of the resolver.
"""

from typing import Any

import pytest

from acquit.graph.index import build_index, detect_roots
from acquit.graph.model import EdgeKind
from acquit.graph.parse import ImportStmt
from acquit.graph.resolve import resolve_import

IMP = EdgeKind.IMPORTS
STAR = EdgeKind.STAR_IMPORT

FLAT = [
    "pkg/__init__.py",
    "pkg/mod.py",
    "pkg/sub/__init__.py",
    "pkg/sub/leaf.py",
    "pkg/sub/other.py",
    "toplevel.py",
]

DEEP = [
    "deep/__init__.py",
    "deep/top.py",
    "deep/x/__init__.py",
    "deep/x/y/__init__.py",
    "deep/x/y/z.py",
]

NS = ["ns/one.py", "ns/two.py", "ns/deeper/three.py"]

SRC = [
    "src/app/__init__.py",
    "src/app/core.py",
    "src/app/util/__init__.py",
    "src/app/util/io.py",
    "tests/test_app.py",
]

SRC2 = ["src/pkg2/__init__.py", "src/pkg2/a.py", "src/pkg2/b.py", "main.py"]

DUP = ["src/dup/__init__.py", "src/dup/m.py", "dup/__init__.py", "dup/m.py"]


def imp(*names: str) -> ImportStmt:
    return ImportStmt(module=None, names=names, level=0, is_star=False)


def frm(module: str, *names: str, level: int = 0) -> ImportStmt:
    return ImportStmt(module=module, names=names, level=level, is_star=False)


def star(module: str, level: int = 0) -> ImportStmt:
    return ImportStmt(module=module, names=("*",), level=level, is_star=True)


def case(
    case_id: str,
    files: list[str],
    importer: str,
    stmt: ImportStmt,
    edges: list[tuple[str, EdgeKind]] | None = None,
    external: list[str] | None = None,
    broken: list[str] | None = None,
    roots: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": case_id,
        "files": files,
        "roots": roots,
        "importer": importer,
        "stmt": stmt,
        "edges": edges or [],
        "external": external or [],
        "broken": broken or [],
    }


CASES: list[dict[str, Any]] = [
    # Absolute chains: importing a.b.c executes every package on the way.
    case(
        "abs-chain-full",
        FLAT,
        "toplevel.py",
        imp("pkg.sub.leaf"),
        edges=[
            ("pkg/__init__.py", IMP),
            ("pkg/sub/__init__.py", IMP),
            ("pkg/sub/leaf.py", IMP),
        ],
    ),
    case(
        "abs-top-level-module",
        FLAT,
        "pkg/mod.py",
        imp("toplevel"),
        edges=[("toplevel.py", IMP)],
    ),
    case(
        "abs-multiple-names-one-stmt",
        FLAT,
        "toplevel.py",
        imp("pkg.mod", "toplevel"),
        edges=[("pkg/__init__.py", IMP), ("pkg/mod.py", IMP), ("toplevel.py", IMP)],
    ),
    case(
        "abs-dedup-overlapping-prefixes",
        FLAT,
        "toplevel.py",
        imp("pkg.mod", "pkg"),
        edges=[("pkg/__init__.py", IMP), ("pkg/mod.py", IMP)],
    ),
    # From-imports: submodule names add an edge, attribute names do not.
    case(
        "from-import-submodule",
        FLAT,
        "toplevel.py",
        frm("pkg.sub", "leaf"),
        edges=[
            ("pkg/__init__.py", IMP),
            ("pkg/sub/__init__.py", IMP),
            ("pkg/sub/leaf.py", IMP),
        ],
    ),
    case(
        "from-import-attribute",
        FLAT,
        "toplevel.py",
        frm("pkg.sub", "helper"),
        edges=[("pkg/__init__.py", IMP), ("pkg/sub/__init__.py", IMP)],
    ),
    case(
        "from-import-subpackage",
        FLAT,
        "toplevel.py",
        frm("pkg", "sub"),
        edges=[("pkg/__init__.py", IMP), ("pkg/sub/__init__.py", IMP)],
    ),
    case(
        "from-import-mixed-submodule-and-attribute",
        FLAT,
        "toplevel.py",
        frm("pkg.sub", "leaf", "helper"),
        edges=[
            ("pkg/__init__.py", IMP),
            ("pkg/sub/__init__.py", IMP),
            ("pkg/sub/leaf.py", IMP),
        ],
    ),
    # Relative imports, level 1 to 3, anchored at the importer's package.
    case(
        "rel1-pure-sibling-module",
        FLAT,
        "pkg/sub/leaf.py",
        frm("", "other", level=1),
        edges=[
            ("pkg/__init__.py", IMP),
            ("pkg/sub/__init__.py", IMP),
            ("pkg/sub/other.py", IMP),
        ],
    ),
    case(
        "rel1-pure-attribute-of-package",
        FLAT,
        "pkg/sub/leaf.py",
        frm("", "helper", level=1),
        edges=[("pkg/__init__.py", IMP), ("pkg/sub/__init__.py", IMP)],
    ),
    case(
        "rel1-named-sibling-module",
        FLAT,
        "pkg/sub/leaf.py",
        frm("other", "x", level=1),
        edges=[
            ("pkg/__init__.py", IMP),
            ("pkg/sub/__init__.py", IMP),
            ("pkg/sub/other.py", IMP),
        ],
    ),
    case(
        "rel2-named-uncle-module",
        FLAT,
        "pkg/sub/leaf.py",
        frm("mod", "y", level=2),
        edges=[("pkg/__init__.py", IMP), ("pkg/mod.py", IMP)],
    ),
    case(
        "rel2-pure-parent-package",
        FLAT,
        "pkg/sub/leaf.py",
        frm("", "mod", level=2),
        edges=[("pkg/__init__.py", IMP), ("pkg/mod.py", IMP)],
    ),
    case(
        "rel3-valid-in-deep-tree",
        DEEP,
        "deep/x/y/z.py",
        frm("top", "t", level=3),
        edges=[("deep/__init__.py", IMP), ("deep/top.py", IMP)],
    ),
    # An __init__.py is its own package: level 1 anchors at the package itself.
    case(
        "rel1-from-init-is-own-package",
        FLAT,
        "pkg/sub/__init__.py",
        frm("", "leaf", level=1),
        edges=[
            ("pkg/__init__.py", IMP),
            ("pkg/sub/__init__.py", IMP),
            ("pkg/sub/leaf.py", IMP),
        ],
    ),
    case(
        "rel1-from-top-init-subpackage",
        FLAT,
        "pkg/__init__.py",
        frm("", "sub", level=1),
        edges=[("pkg/__init__.py", IMP), ("pkg/sub/__init__.py", IMP)],
    ),
    case(
        "rel2-from-init-anchors-at-parent",
        FLAT,
        "pkg/sub/__init__.py",
        frm("mod", "y", level=2),
        edges=[("pkg/__init__.py", IMP), ("pkg/mod.py", IMP)],
    ),
    # Climbing above the root is broken, recorded in dotted-text form.
    case(
        "rel3-beyond-root",
        FLAT,
        "pkg/sub/leaf.py",
        frm("x", "y", level=3),
        broken=["...x"],
    ),
    case(
        "rel1-beyond-root-from-top-module",
        FLAT,
        "toplevel.py",
        frm("", "pkg", level=1),
        broken=[".pkg"],
    ),
    case(
        "rel2-beyond-root-pure-multiple-names",
        FLAT,
        "toplevel.py",
        frm("", "a", "b", level=2),
        broken=["..a", "..b"],
    ),
    case(
        "rel1-missing-sibling-module-broken",
        FLAT,
        "pkg/sub/leaf.py",
        frm("missing", "x", level=1),
        edges=[("pkg/__init__.py", IMP), ("pkg/sub/__init__.py", IMP)],
        broken=["pkg.sub.missing"],
    ),
    # Star imports: into a package they pull in every direct submodule.
    case(
        "star-into-package",
        FLAT,
        "toplevel.py",
        star("pkg.sub"),
        edges=[
            ("pkg/__init__.py", IMP),
            ("pkg/sub/__init__.py", IMP),
            ("pkg/sub/leaf.py", STAR),
            ("pkg/sub/other.py", STAR),
        ],
    ),
    case(
        "star-into-package-includes-subpackages",
        FLAT,
        "toplevel.py",
        star("pkg"),
        edges=[
            ("pkg/__init__.py", IMP),
            ("pkg/mod.py", STAR),
            ("pkg/sub/__init__.py", STAR),
        ],
    ),
    case(
        "star-into-plain-module",
        FLAT,
        "toplevel.py",
        star("pkg.mod"),
        edges=[("pkg/__init__.py", IMP), ("pkg/mod.py", IMP)],
    ),
    case(
        "star-relative-own-package",
        FLAT,
        "pkg/sub/leaf.py",
        star("", level=1),
        edges=[
            ("pkg/__init__.py", IMP),
            ("pkg/sub/__init__.py", IMP),
            ("pkg/sub/leaf.py", STAR),
            ("pkg/sub/other.py", STAR),
        ],
    ),
    case(
        "star-into-namespace-package",
        NS,
        "app.py",
        star("ns"),
        edges=[("ns/one.py", STAR), ("ns/two.py", STAR)],
    ),
    case(
        "star-into-external",
        FLAT,
        "toplevel.py",
        star("numpy"),
        external=["numpy"],
    ),
    # Namespace packages: no __init__ to execute, contents still resolve.
    case(
        "namespace-absolute-chain",
        NS,
        "app.py",
        imp("ns.deeper.three"),
        edges=[("ns/deeper/three.py", IMP)],
    ),
    case(
        "namespace-from-import-submodule",
        NS,
        "app.py",
        frm("ns", "one"),
        edges=[("ns/one.py", IMP)],
    ),
    case(
        "namespace-missing-name-assumed-attribute",
        NS,
        "app.py",
        frm("ns", "missing"),
    ),
    # src layout, flat layout, and their interaction under auto-detection.
    case(
        "src-layout-auto-detected",
        SRC,
        "tests/test_app.py",
        imp("app.core"),
        edges=[("src/app/__init__.py", IMP), ("src/app/core.py", IMP)],
    ),
    case(
        "src-layout-import-via-repo-root-prefix",
        SRC,
        "tests/test_app.py",
        imp("src.app.core"),
        edges=[("src/app/__init__.py", IMP), ("src/app/core.py", IMP)],
    ),
    case(
        "explicit-src-root-excludes-repo-root",
        SRC,
        "src/app/core.py",
        imp("tests.test_app"),
        roots=["src"],
        external=["tests"],
    ),
    case(
        "collision-across-roots-all-candidates",
        DUP,
        "main.py",
        imp("dup.m"),
        roots=["src", ""],
        edges=[
            ("dup/__init__.py", IMP),
            ("dup/m.py", IMP),
            ("src/dup/__init__.py", IMP),
            ("src/dup/m.py", IMP),
        ],
    ),
    case(
        "rel1-multi-identity-importer",
        SRC2,
        "src/pkg2/a.py",
        frm("", "b", level=1),
        edges=[("src/pkg2/__init__.py", IMP), ("src/pkg2/b.py", IMP)],
    ),
    # Level 2 from a depth-1 package: beyond root under the src identity, so
    # the statement is recorded broken even though the repo-root identity
    # anchors at the src namespace dir.
    case(
        "rel2-partial-beyond-root-still-broken",
        SRC2,
        "src/pkg2/a.py",
        frm("", "b", level=2),
        broken=["..b"],
    ),
    # Unresolvable names: first-party tops are broken, everything else external.
    case(
        "broken-first-party-plain-import",
        FLAT,
        "toplevel.py",
        imp("pkg.nothere"),
        edges=[("pkg/__init__.py", IMP)],
        broken=["pkg.nothere"],
    ),
    case(
        "broken-first-party-from-import",
        FLAT,
        "toplevel.py",
        frm("pkg.nope", "thing"),
        edges=[("pkg/__init__.py", IMP)],
        broken=["pkg.nope"],
    ),
    case(
        "external-plain-import",
        FLAT,
        "toplevel.py",
        imp("numpy"),
        external=["numpy"],
    ),
    case(
        "external-dotted-records-top-level",
        FLAT,
        "toplevel.py",
        imp("os.path"),
        external=["os"],
    ),
    case(
        "external-from-import",
        FLAT,
        "toplevel.py",
        frm("collections.abc", "Mapping"),
        external=["collections"],
    ),
    case(
        "external-and-first-party-in-one-stmt",
        FLAT,
        "toplevel.py",
        imp("json", "pkg.mod"),
        edges=[("pkg/__init__.py", IMP), ("pkg/mod.py", IMP)],
        external=["json"],
    ),
]


@pytest.mark.parametrize("c", CASES, ids=[str(c["id"]) for c in CASES])
def test_resolve_corpus(c: dict[str, Any]) -> None:
    roots = detect_roots(c["files"], c["roots"])
    idx = build_index(c["files"], roots)
    result = resolve_import(c["stmt"], c["importer"], idx)
    assert result.edges == tuple(sorted(c["edges"]))
    assert result.external_top_levels == tuple(sorted(c["external"]))
    assert result.broken_first_party == tuple(sorted(c["broken"]))


def test_case_ids_are_unique() -> None:
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
