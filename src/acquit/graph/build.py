"""Assembly of classified files and parsed facts into one BuiltGraph.

Pure function over already-collected inputs: no filesystem access, no user
code execution. Node insertion order, edge order, and the graph hash are
fully determined by the inputs, so byte-identical trees produce
byte-identical graphs on every platform.
"""

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import rustworkx as rx

from acquit.graph.index import ModuleIndex
from acquit.graph.model import (
    GRAPH_SCHEMA_VERSION,
    BuiltGraph,
    Edge,
    EdgeKind,
    Node,
    NodeKind,
)
from acquit.graph.parse import ImportStmt, ModuleFacts
from acquit.graph.resolve import resolve_import
from acquit.pytestmap.conftree import ConftestFacts, conftest_scope_edges
from acquit.pytestmap.pytestcfg import PytestConfig

EXTERNAL_PREFIX: Final = "ext:"

_PY_SUFFIX: Final = ".py"
_STUB_SUFFIX: Final = ".pyi"


@dataclass(slots=True)
class _Acc:
    edges: set[Edge] = field(default_factory=set)
    tainted: set[str] = field(default_factory=set)
    externals: set[str] = field(default_factory=set)

    def external_edge(self, src: str, top_level: str, kind: EdgeKind) -> None:
        self.externals.add(top_level)
        self.edges.add(Edge(src=src, dst=EXTERNAL_PREFIX + top_level, kind=kind))


def assemble_graph(
    files: Sequence[str],
    kinds: Mapping[str, NodeKind],
    facts: Mapping[str, ModuleFacts],
    unparseable: Collection[str],
    index: ModuleIndex,
    conftest_facts: Mapping[str, ConftestFacts],
    pytest_config: PytestConfig,
) -> BuiltGraph:
    """Assemble the dependency graph for one analyzed tree.

    Every file becomes a node; external imports become on-demand "ext:" nodes.
    A node is tainted when its facts carry suspects, its source failed to
    parse, or any of its imports look first-party but do not resolve.
    """
    acc = _Acc()
    acc.tainted.update(unparseable)
    for module in facts.values():
        _collect_module(module, index, acc)

    file_paths = sorted(set(files))
    tests = [path for path in file_paths if kinds[path] is NodeKind.TEST]
    conftests = [path for path in file_paths if kinds[path] is NodeKind.CONFTEST]
    for test in tests:
        acc.edges.update(conftest_scope_edges(test, conftests))

    for conftest in conftest_facts.values():
        for plugin in conftest.pytest_plugins:
            for target in _plugin_targets(plugin, index):
                acc.edges.add(Edge(src=conftest.path, dst=target, kind=EdgeKind.PLUGIN))
    # pytest honors pytest_plugins declared in test modules too.
    for test in tests:
        test_facts = facts.get(test)
        if test_facts is None:
            continue
        for plugin in test_facts.pytest_plugins_decl:
            for target in _plugin_targets(plugin, index):
                acc.edges.add(Edge(src=test, dst=target, kind=EdgeKind.PLUGIN))
    for plugin in pytest_config.extra_plugins:
        for target in _plugin_targets(plugin, index):
            for test in tests:
                acc.edges.add(Edge(src=test, dst=target, kind=EdgeKind.PLUGIN))

    present = set(file_paths)
    for path in file_paths:
        if path.endswith(_PY_SUFFIX):
            stub = path.removesuffix(_PY_SUFFIX) + _STUB_SUFFIX
            if stub in present:
                acc.edges.add(Edge(src=path, dst=stub, kind=EdgeKind.STUB_OF))

    return _build(file_paths, kinds, index, acc)


def _absolute_import(name: str) -> ImportStmt:
    return ImportStmt(module=None, names=(name,), level=0, is_star=False)


def _collect_module(module: ModuleFacts, index: ModuleIndex, acc: _Acc) -> None:
    src = module.path
    if module.suspects:
        acc.tainted.add(src)
    for stmt in module.imports:
        resolution = resolve_import(stmt, src, index)
        for dst, kind in resolution.edges:
            acc.edges.add(Edge(src=src, dst=dst, kind=kind))
        for top_level in resolution.external_top_levels:
            acc.external_edge(src, top_level, EdgeKind.IMPORTS)
        if resolution.broken_first_party:
            acc.tainted.add(src)
    for name in module.dyn_literal_imports:
        if not name:
            continue
        resolution = resolve_import(_absolute_import(name), src, index)
        for dst, _ in resolution.edges:
            acc.edges.add(Edge(src=src, dst=dst, kind=EdgeKind.DYNAMIC_IMPORT))
        for top_level in resolution.external_top_levels:
            acc.external_edge(src, top_level, EdgeKind.DYNAMIC_IMPORT)
        if resolution.broken_first_party:
            acc.tainted.add(src)


def _plugin_targets(name: str, index: ModuleIndex) -> tuple[str, ...]:
    # Plugin names are absolute, so the importer path is irrelevant here.
    resolution = resolve_import(_absolute_import(name), "", index)
    if resolution.broken_first_party or resolution.external_top_levels:
        return ()
    return tuple(path for path, _ in resolution.edges)


def _build(
    file_paths: Sequence[str],
    kinds: Mapping[str, NodeKind],
    index: ModuleIndex,
    acc: _Acc,
) -> BuiltGraph:
    dotted_by_path: dict[str, list[str]] = {}
    for dotted, paths in index.by_dotted.items():
        for path in paths:
            dotted_by_path.setdefault(path, []).append(dotted)

    node_list = [
        Node(
            path=path,
            kind=kinds[path],
            tainted=path in acc.tainted,
            dotted_names=tuple(sorted(dotted_by_path.get(path, ()))),
        )
        for path in file_paths
    ]
    node_list.extend(
        Node(path=EXTERNAL_PREFIX + top_level, kind=NodeKind.EXTERNAL)
        for top_level in sorted(acc.externals)
    )

    edges = sorted(acc.edges, key=lambda item: (item.src, item.dst, item.kind))
    digraph: rx.PyDiGraph[Node, EdgeKind] = rx.PyDiGraph()
    index_of = {node.path: digraph.add_node(node) for node in node_list}
    for edge in edges:
        digraph.add_edge(index_of[edge.src], index_of[edge.dst], edge.kind)
    return BuiltGraph(
        digraph=digraph,
        index_of=index_of,
        nodes={node.path: node for node in node_list},
        graph_hash=_graph_hash(node_list, edges),
    )


def _graph_hash(nodes: Sequence[Node], edges: Sequence[Edge]) -> str:
    hasher = hashlib.sha256()
    hasher.update(f"acquit-graph-v{GRAPH_SCHEMA_VERSION}\n".encode())
    for node in sorted(nodes, key=lambda item: item.path):
        hasher.update(f"N\t{node.path}\t{node.kind.value}\t{int(node.tainted)}\n".encode())
    for edge in sorted(edges, key=lambda item: (item.src, item.dst, item.kind)):
        hasher.update(f"E\t{edge.src}\t{edge.dst}\t{edge.kind.value}\n".encode())
    return hasher.hexdigest()
