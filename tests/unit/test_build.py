"""Unit tests for graph assembly over a synthetic project built from inline sources."""

from collections.abc import Collection, Mapping, Sequence

from acquit.graph.build import assemble_graph
from acquit.graph.index import build_index
from acquit.graph.model import BuiltGraph, EdgeKind, NodeKind
from acquit.graph.parse import parse_module_facts
from acquit.pytestmap.conftree import inspect_conftest
from acquit.pytestmap.discover import classify_file, discover_test_files
from acquit.pytestmap.pytestcfg import DEFAULT_NORECURSEDIRS, DEFAULT_PYTHON_FILES, PytestConfig

SOURCES: dict[str, str] = {
    "conftest.py": "pytest_plugins = ['app.plug']\n",
    "src/app/__init__.py": "",
    "src/app/broken.py": "import app.missing\n",
    "src/app/core.py": "import json\n",
    "src/app/dyn.py": "import importlib\nimportlib.import_module('app.core')\n",
    "src/app/dyn2.py": "__import__('app.nothing')\n__import__('yaml')\n",
    "src/app/plug.py": "",
    "src/app/tainted.py": "import sys\nsys.path.append('elsewhere')\n",
    "tests/conftest.py": "",
    "tests/test_core.py": "import json\nimport app.core\n",
    "tests/test_dyn.py": "import app.dyn\n",
}

UNPARSEABLE_PATH = "src/app/legacy.py"

EXTRA_FILES = [
    UNPARSEABLE_PATH,
    "src/app/core.pyi",
    "pyproject.toml",
    "data/fixture.json",
]

FILES: list[str] = sorted([*SOURCES, *EXTRA_FILES])

TEST_PATHS = ("tests/test_core.py", "tests/test_dyn.py")


def make_config(extra_plugins: tuple[str, ...] = ()) -> PytestConfig:
    return PytestConfig(
        source="pyproject.toml",
        python_files=DEFAULT_PYTHON_FILES,
        testpaths=(),
        norecursedirs=DEFAULT_NORECURSEDIRS,
        addopts=(),
        pythonpath=(),
        doctest_modules=False,
        extra_plugins=extra_plugins,
    )


def assemble(
    *,
    files: Sequence[str] | None = None,
    sources: Mapping[str, str] | None = None,
    unparseable: Collection[str] = (UNPARSEABLE_PATH,),
    extra_plugins: tuple[str, ...] = (),
    permute: bool = False,
) -> BuiltGraph:
    files = FILES if files is None else files
    sources = SOURCES if sources is None else sources
    cfg = make_config(extra_plugins)
    test_files = frozenset(discover_test_files(files, cfg))
    kinds = {path: classify_file(path, cfg, test_files) for path in files}
    facts = {path: parse_module_facts(text.encode(), path) for path, text in sources.items()}
    conftest_facts = {
        path: inspect_conftest(text.encode(), path)
        for path, text in sources.items()
        if path.rsplit("/", 1)[-1] == "conftest.py"
    }
    if permute:
        files = list(reversed(files))
        kinds = dict(reversed(list(kinds.items())))
        facts = dict(reversed(list(facts.items())))
        conftest_facts = dict(reversed(list(conftest_facts.items())))
        unparseable = set(unparseable)
    index = build_index(files, ["src", ""])
    return assemble_graph(
        files=files,
        kinds=kinds,
        facts=facts,
        unparseable=unparseable,
        index=index,
        conftest_facts=conftest_facts,
        pytest_config=cfg,
    )


def edge_set(graph: BuiltGraph) -> set[tuple[str, str, EdgeKind]]:
    dg = graph.digraph
    return {(dg[u].path, dg[v].path, kind) for u, v, kind in dg.weighted_edge_list()}


def test_every_file_gets_a_node_with_its_kind() -> None:
    graph = assemble()
    assert graph.nodes["tests/test_core.py"].kind is NodeKind.TEST
    assert graph.nodes["conftest.py"].kind is NodeKind.CONFTEST
    assert graph.nodes["src/app/core.py"].kind is NodeKind.MODULE
    assert graph.nodes["src/app/core.pyi"].kind is NodeKind.STUB
    assert graph.nodes["pyproject.toml"].kind is NodeKind.CONFIG
    assert graph.nodes["data/fixture.json"].kind is NodeKind.RESOURCE
    assert set(FILES) <= set(graph.nodes)


def test_external_nodes_created_on_demand() -> None:
    graph = assemble()
    externals = {path for path, node in graph.nodes.items() if node.kind is NodeKind.EXTERNAL}
    assert externals == {"ext:importlib", "ext:json", "ext:sys", "ext:yaml"}


def test_taint_from_suspects() -> None:
    assert assemble().nodes["src/app/tainted.py"].tainted


def test_taint_from_unparseable() -> None:
    assert assemble().nodes[UNPARSEABLE_PATH].tainted


def test_taint_from_broken_first_party_import() -> None:
    assert assemble().nodes["src/app/broken.py"].tainted


def test_taint_from_unresolved_first_party_dynamic_import() -> None:
    assert assemble().nodes["src/app/dyn2.py"].tainted


def test_clean_files_are_untainted() -> None:
    graph = assemble()
    tainted = {path for path, node in graph.nodes.items() if node.tainted}
    assert tainted == {
        UNPARSEABLE_PATH,
        "src/app/broken.py",
        "src/app/dyn2.py",
        "src/app/tainted.py",
    }


def test_prefix_chain_edges_for_dotted_import() -> None:
    edges = edge_set(assemble())
    assert ("tests/test_core.py", "src/app/__init__.py", EdgeKind.IMPORTS) in edges
    assert ("tests/test_core.py", "src/app/core.py", EdgeKind.IMPORTS) in edges


def test_conftest_scope_edges_only_from_tests() -> None:
    edges = edge_set(assemble())
    scoped = {(src, dst) for src, dst, kind in edges if kind is EdgeKind.CONFTEST_SCOPE}
    assert scoped == {
        (test, conftest) for test in TEST_PATHS for conftest in ("conftest.py", "tests/conftest.py")
    }


def test_stub_sibling_edge() -> None:
    edges = edge_set(assemble())
    stubbed = {(src, dst) for src, dst, kind in edges if kind is EdgeKind.STUB_OF}
    assert stubbed == {("src/app/core.py", "src/app/core.pyi")}


def test_external_node_reused_across_importers() -> None:
    graph = assemble()
    edges = edge_set(graph)
    importers = {src for src, dst, _ in edges if dst == "ext:json"}
    assert importers == {"src/app/core.py", "tests/test_core.py"}
    assert sum(1 for path in graph.nodes if path == "ext:json") == 1


def test_dynamic_literal_import_edge_kind() -> None:
    edges = edge_set(assemble())
    assert ("src/app/dyn.py", "src/app/__init__.py", EdgeKind.DYNAMIC_IMPORT) in edges
    assert ("src/app/dyn.py", "src/app/core.py", EdgeKind.DYNAMIC_IMPORT) in edges
    assert ("src/app/dyn2.py", "ext:yaml", EdgeKind.DYNAMIC_IMPORT) in edges


def test_conftest_plugin_edges() -> None:
    edges = edge_set(assemble())
    plugin = {(src, dst) for src, dst, kind in edges if kind is EdgeKind.PLUGIN}
    assert plugin == {
        ("conftest.py", "src/app/__init__.py"),
        ("conftest.py", "src/app/plug.py"),
    }


def test_extra_plugins_edges_from_every_test() -> None:
    baseline = edge_set(assemble())
    edges = edge_set(assemble(extra_plugins=("app.plug",)))
    assert edges - baseline == {
        (test, dst, EdgeKind.PLUGIN)
        for test in TEST_PATHS
        for dst in ("src/app/__init__.py", "src/app/plug.py")
    }


def test_unresolvable_plugins_ignored() -> None:
    baseline = assemble()
    sources = dict(SOURCES) | {"conftest.py": "pytest_plugins = ['nope.plugin', 'app.nope']\n"}
    graph = assemble(sources=sources, extra_plugins=("pytest_cov", "app.nope"))
    assert not any(kind is EdgeKind.PLUGIN for _, _, kind in edge_set(graph))
    assert "ext:nope" not in graph.nodes
    assert "ext:pytest_cov" not in graph.nodes
    assert not graph.nodes["conftest.py"].tainted
    tainted = {path for path, node in graph.nodes.items() if node.tainted}
    assert tainted == {path for path, node in baseline.nodes.items() if node.tainted}


def test_full_edge_set() -> None:
    assert edge_set(assemble()) == {
        ("conftest.py", "src/app/__init__.py", EdgeKind.PLUGIN),
        ("conftest.py", "src/app/plug.py", EdgeKind.PLUGIN),
        ("src/app/broken.py", "src/app/__init__.py", EdgeKind.IMPORTS),
        ("src/app/core.py", "ext:json", EdgeKind.IMPORTS),
        ("src/app/core.py", "src/app/core.pyi", EdgeKind.STUB_OF),
        ("src/app/dyn.py", "ext:importlib", EdgeKind.IMPORTS),
        ("src/app/dyn.py", "src/app/__init__.py", EdgeKind.DYNAMIC_IMPORT),
        ("src/app/dyn.py", "src/app/core.py", EdgeKind.DYNAMIC_IMPORT),
        ("src/app/dyn2.py", "ext:yaml", EdgeKind.DYNAMIC_IMPORT),
        ("src/app/dyn2.py", "src/app/__init__.py", EdgeKind.DYNAMIC_IMPORT),
        ("src/app/tainted.py", "ext:sys", EdgeKind.IMPORTS),
        ("tests/test_core.py", "conftest.py", EdgeKind.CONFTEST_SCOPE),
        ("tests/test_core.py", "ext:json", EdgeKind.IMPORTS),
        ("tests/test_core.py", "src/app/__init__.py", EdgeKind.IMPORTS),
        ("tests/test_core.py", "src/app/core.py", EdgeKind.IMPORTS),
        ("tests/test_core.py", "tests/conftest.py", EdgeKind.CONFTEST_SCOPE),
        ("tests/test_dyn.py", "conftest.py", EdgeKind.CONFTEST_SCOPE),
        ("tests/test_dyn.py", "src/app/__init__.py", EdgeKind.IMPORTS),
        ("tests/test_dyn.py", "src/app/dyn.py", EdgeKind.IMPORTS),
        ("tests/test_dyn.py", "tests/conftest.py", EdgeKind.CONFTEST_SCOPE),
    }


def test_node_insertion_order_is_files_then_externals() -> None:
    graph = assemble()
    paths = [graph.digraph[i].path for i in graph.digraph.node_indices()]
    assert paths == [*FILES, "ext:importlib", "ext:json", "ext:sys", "ext:yaml"]


def test_lookups_are_consistent() -> None:
    graph = assemble()
    assert set(graph.index_of) == set(graph.nodes)
    for path, i in graph.index_of.items():
        assert graph.digraph[i] is graph.nodes[path]
        assert graph.nodes[path].path == path


def test_dotted_names_attached_from_index() -> None:
    graph = assemble()
    assert graph.nodes["src/app/core.py"].dotted_names == ("app.core", "src.app.core")
    assert graph.nodes["conftest.py"].dotted_names == ("conftest",)
    assert graph.nodes["data/fixture.json"].dotted_names == ()


def test_assembly_is_deterministic() -> None:
    first = assemble()
    second = assemble()
    assert first.graph_hash == second.graph_hash
    assert list(first.digraph.weighted_edge_list()) == list(second.digraph.weighted_edge_list())
    assert list(first.digraph.nodes()) == list(second.digraph.nodes())


def test_hash_changes_when_taint_flips() -> None:
    tainted_plug = assemble(unparseable=(UNPARSEABLE_PATH, "src/app/plug.py"))
    assert assemble().graph_hash != tainted_plug.graph_hash


def test_hash_changes_when_an_edge_changes() -> None:
    sources = dict(SOURCES) | {"src/app/plug.py": "import app.core\n"}
    assert assemble().graph_hash != assemble(sources=sources).graph_hash


def test_hash_stable_under_input_permutations() -> None:
    first = assemble()
    second = assemble(permute=True)
    assert first.graph_hash == second.graph_hash
    assert list(first.digraph.nodes()) == list(second.digraph.nodes())
    assert list(first.digraph.weighted_edge_list()) == list(second.digraph.weighted_edge_list())
