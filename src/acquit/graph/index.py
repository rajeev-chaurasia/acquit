"""Dotted-name index over import roots.

Built from a repository file listing, never the filesystem, so it stays pure
and testable. A dotted name may resolve to several files when roots overlap or
collide; every candidate is kept, and the resolver treats each one as a real
dependency.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_SRC = "src"
_INIT = "__init__.py"


@dataclass(frozen=True, slots=True)
class ModuleIndex:
    """Lookup table from dotted module names to candidate file paths."""

    roots: tuple[str, ...]
    by_dotted: Mapping[str, tuple[str, ...]]
    first_party_top_levels: frozenset[str]


def detect_roots(files: Sequence[str], explicit: Sequence[str] | None = None) -> tuple[str, ...]:
    """Choose import roots for a repository.

    Explicit roots win. Otherwise "src" is used when files live under it, and
    the repo root ("") is added when anything importable lives outside src.
    """
    if explicit is not None:
        return _normalize_roots(explicit)
    roots: list[str] = []
    if any(path.startswith(_SRC + "/") for path in files):
        roots.append(_SRC)
    if any(not path.startswith(_SRC + "/") for path in files) or not roots:
        roots.append("")
    return tuple(roots)


def build_index(files: Sequence[str], roots: Sequence[str]) -> ModuleIndex:
    """Map every dotted name importable from the given roots to its files.

    Directories without __init__.py still yield dotted names for their
    contents, matching PEP 420 namespace package semantics.
    """
    normalized = _normalize_roots(roots)
    candidates: dict[str, set[str]] = {}
    tops: set[str] = set()
    for path in files:
        if not path.endswith(".py"):
            continue
        for root in normalized:
            relative = _relative_to_root(path, root)
            if relative is None:
                continue
            dotted = _dotted_from_relative(relative)
            if not dotted:
                continue
            candidates.setdefault(dotted, set()).add(path)
            tops.add(dotted.partition(".")[0])
    by_dotted = {name: tuple(sorted(paths)) for name, paths in sorted(candidates.items())}
    return ModuleIndex(
        roots=normalized,
        by_dotted=by_dotted,
        first_party_top_levels=frozenset(tops),
    )


def _normalize_roots(roots: Sequence[str]) -> tuple[str, ...]:
    out: list[str] = []
    for root in roots:
        clean = root.strip("/")
        if clean.startswith("./"):
            clean = clean[2:]
        if clean == ".":
            clean = ""
        if clean not in out:
            out.append(clean)
    return tuple(out)


def _relative_to_root(path: str, root: str) -> str | None:
    if root == "":
        return path
    if path.startswith(root + "/"):
        return path[len(root) + 1 :]
    return None


def _dotted_from_relative(relative: str) -> str:
    # An __init__.py directly under a root names no module; callers skip "".
    if relative == _INIT:
        return ""
    if relative.endswith("/" + _INIT):
        return relative[: -len("/" + _INIT)].replace("/", ".")
    return relative[: -len(".py")].replace("/", ".")
