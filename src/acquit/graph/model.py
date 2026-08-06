"""Typed dependency graph model.

Node identity is the repo-relative POSIX path. Dotted module names are attributes,
not identities, so name collisions across import roots stay unambiguous.
Edge direction: A -> B means "A depends on B".
"""

from dataclasses import dataclass, field
from enum import StrEnum


class NodeKind(StrEnum):
    MODULE = "module"
    TEST = "test"
    CONFTEST = "conftest"
    STUB = "stub"
    CONFIG = "config"
    RESOURCE = "resource"
    EXTERNAL = "external"


class EdgeKind(StrEnum):
    IMPORTS = "imports"
    DYNAMIC_IMPORT = "dynamic-import"
    STAR_IMPORT = "star-import"
    CONFTEST_SCOPE = "conftest-scope"
    PLUGIN = "plugin"
    STUB_OF = "stub-of"


@dataclass(frozen=True, slots=True)
class Node:
    path: str
    kind: NodeKind
    # A tainted node has dependencies we cannot know statically. Any test that
    # reaches one must always run, regardless of the diff.
    tainted: bool = False
    dotted_names: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Edge:
    src: str
    dst: str
    kind: EdgeKind
