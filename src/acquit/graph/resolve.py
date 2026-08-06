"""Resolution of one import statement into dependency edges.

This is the soundness-critical step: every file a statement could execute must
become an edge, so unresolved names are classified instead of dropped. Names
that look first-party but do not resolve are reported as broken, which the
policy engine treats as a reason to run everything.
"""

from dataclasses import dataclass

from acquit.graph.index import ModuleIndex
from acquit.graph.model import EdgeKind
from acquit.graph.parse import ImportStmt

_INIT_SUFFIX = "/__init__.py"


@dataclass(frozen=True, slots=True)
class Resolution:
    """Edges and unresolved-name classifications for one import statement."""

    edges: tuple[tuple[str, EdgeKind], ...]
    external_top_levels: tuple[str, ...]
    broken_first_party: tuple[str, ...]


@dataclass(slots=True)
class _Acc:
    edges: set[tuple[str, EdgeKind]]
    external: set[str]
    broken: set[str]


def resolve_import(stmt: ImportStmt, importer_path: str, idx: ModuleIndex) -> Resolution:
    """Resolve one import statement from the given importer against the index."""
    acc = _Acc(edges=set(), external=set(), broken=set())
    if stmt.level > 0:
        _resolve_relative(stmt, importer_path, idx, acc)
    elif stmt.module is None:
        for name in stmt.names:
            _resolve_absolute_chain(name, idx, acc)
    else:
        _resolve_from(stmt.module, stmt.names, stmt.is_star, idx, acc)
    return Resolution(
        edges=tuple(sorted(acc.edges)),
        external_top_levels=tuple(sorted(acc.external)),
        broken_first_party=tuple(sorted(acc.broken)),
    )


def _resolve_absolute_chain(name: str, idx: ModuleIndex, acc: _Acc) -> None:
    # Importing a.b.c executes a/__init__.py and a/b/__init__.py too.
    _prefix_edges(name, idx, acc)
    if not _exists(name, idx):
        _record_unresolved(name, idx, acc)


def _resolve_from(
    base: str, names: tuple[str, ...], is_star: bool, idx: ModuleIndex, acc: _Acc
) -> None:
    _prefix_edges(base, idx, acc)
    if not _exists(base, idx):
        _record_unresolved(base, idx, acc)
        return
    if is_star:
        _expand_star(base, idx, acc)
        return
    for name in names:
        # A name that is not a submodule is assumed to be an attribute; the
        # edge to the base module covers re-exports transitively.
        for path in idx.by_dotted.get(f"{base}.{name}", ()):
            acc.edges.add((path, EdgeKind.IMPORTS))


def _resolve_relative(stmt: ImportStmt, importer_path: str, idx: ModuleIndex, acc: _Acc) -> None:
    identities = _package_parts(importer_path, idx.roots)
    anchored: set[str] = set()
    beyond_root = not identities
    for parts in identities:
        keep = len(parts) - stmt.level + 1
        if keep <= 0:
            beyond_root = True
            continue
        anchor = ".".join(parts[:keep])
        anchored.add(f"{anchor}.{stmt.module}" if stmt.module else anchor)
    if beyond_root:
        # Recorded even when another root anchors, so a level that is illegal
        # under any identity still taints the importer (fail closed).
        dots = "." * stmt.level
        if stmt.module:
            acc.broken.add(dots + stmt.module)
        else:
            for name in stmt.names:
                acc.broken.add(dots + name)
    if not anchored:
        return
    existing = {base for base in anchored if _exists(base, idx)}
    if not existing:
        for base in anchored:
            _prefix_edges(base, idx, acc)
            acc.broken.add(base)
        return
    for base in existing:
        _resolve_from(base, stmt.names, stmt.is_star, idx, acc)


def _package_parts(importer_path: str, roots: tuple[str, ...]) -> list[tuple[str, ...]]:
    # Dropping the filename makes an __init__.py anchor at its own package.
    out: list[tuple[str, ...]] = []
    for root in roots:
        if root == "":
            relative = importer_path
        elif importer_path.startswith(root + "/"):
            relative = importer_path[len(root) + 1 :]
        else:
            continue
        parts = tuple(relative.split("/")[:-1])
        if parts not in out:
            out.append(parts)
    return out


def _prefix_edges(dotted: str, idx: ModuleIndex, acc: _Acc) -> None:
    parts = dotted.split(".")
    for end in range(1, len(parts) + 1):
        for path in idx.by_dotted.get(".".join(parts[:end]), ()):
            acc.edges.add((path, EdgeKind.IMPORTS))


def _expand_star(base: str, idx: ModuleIndex, acc: _Acc) -> None:
    if not _is_package(base, idx):
        return
    prefix = base + "."
    for dotted, paths in idx.by_dotted.items():
        if dotted.startswith(prefix) and "." not in dotted[len(prefix) :]:
            for path in paths:
                acc.edges.add((path, EdgeKind.STAR_IMPORT))


def _is_package(base: str, idx: ModuleIndex) -> bool:
    candidates = idx.by_dotted.get(base, ())
    if any(path.endswith(_INIT_SUFFIX) for path in candidates):
        return True
    return _has_children(base, idx)


def _exists(dotted: str, idx: ModuleIndex) -> bool:
    return dotted in idx.by_dotted or _has_children(dotted, idx)


def _has_children(dotted: str, idx: ModuleIndex) -> bool:
    prefix = dotted + "."
    return any(name.startswith(prefix) for name in idx.by_dotted)


def _record_unresolved(dotted: str, idx: ModuleIndex, acc: _Acc) -> None:
    top = dotted.partition(".")[0]
    if top in idx.first_party_top_levels:
        acc.broken.add(dotted)
    else:
        acc.external.add(top)
