"""Typed dependency graph model.

Node identity is the repo-relative POSIX path. Dotted module names are attributes,
not identities, so name collisions across import roots stay unambiguous.
Edge direction: A -> B means "A depends on B".
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

import rustworkx as rx

from acquit.graph.resolvers.checkers import ReexportTier

# Bump whenever the canonical (nodes, edges) form changes meaning: hashes
# anchor witnesses and replay, so old graphs must not verify against new
# semantics. Version 2 added INIT_REEXPORT edges for pure re-exporter inits
# (ADR 0008); edge kinds are hashed.
GRAPH_SCHEMA_VERSION: Final = 2


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
    # An outgoing import edge of a proven pure re-exporter __init__.py: the
    # dependency is real but import-time-only for consumers bound to other
    # symbols (ADR 0008). Inert by construction in this wave: selection
    # treats every edge kind alike, so these participate in reachability
    # exactly like IMPORTS and closures keep their full over-approximation;
    # only a later impact rule may treat them conditionally.
    INIT_REEXPORT = "init-reexport"


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


@dataclass(frozen=True, slots=True)
class BuiltGraph:
    """The assembled dependency graph plus the lookups everything downstream needs.

    Node payloads are Node, edge payloads are EdgeKind. The hash covers the
    canonical (nodes, edges, schema version) form and anchors witnesses,
    caching, and replay verification.
    """

    digraph: rx.PyDiGraph[Node, EdgeKind]
    index_of: Mapping[str, int]
    nodes: Mapping[str, Node]
    graph_hash: str
    # Proven pure re-exporter inits and the whitelist tier each passed. The
    # INIT_REEXPORT edge kind already names the init a path crosses (it is
    # the edge source); this map carries the tier a wave-2 witness must
    # re-verify at both revisions. Derivable data stays out of the hash.
    reexport_inits: Mapping[str, ReexportTier] = field(default_factory=dict)
