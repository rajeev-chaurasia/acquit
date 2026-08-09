"""Unit tests for re-export narrowing in graph assembly (ADR 0008, wave one).

The contract under test: a proven pure re-exporter init gets its outgoing
import edges annotated INIT_REEXPORT, consumers keep full edges to the init,
every prefix, and their symbols' homes, and every degraded path reproduces
today's graph exactly. Closures never shrink; selection treats the new kind
like IMPORTS until the wave-two impact rule lands.
"""

from collections.abc import Mapping, Sequence

from acquit.graph.build import assemble_graph
from acquit.graph.index import build_index
from acquit.graph.model import BuiltGraph, EdgeKind
from acquit.graph.parse import parse_module_facts
from acquit.graph.resolvers.checkers import ReexportTier
from acquit.pytestmap.conftree import inspect_conftest
from acquit.pytestmap.discover import classify_file, discover_test_files
from acquit.pytestmap.pytestcfg import DEFAULT_NORECURSEDIRS, DEFAULT_PYTHON_FILES, PytestConfig

PURE_INIT = (
    '"""pkg"""\n'
    "from .table import Table\n"
    "from .console import Console\n"
    '__all__ = ["Table", "Console"]\n'
    '__version__ = "1.0"\n'
)

SOURCES: dict[str, str] = {
    "pkg/__init__.py": PURE_INIT,
    "pkg/table.py": "class Table: ...\n",
    "pkg/console.py": "_THEMES = dict(plain='')\nclass Console: ...\n",
    "tests/test_table.py": "from pkg import Table\n",
}


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


def assemble(sources: Mapping[str, str], roots: Sequence[str] = ("",)) -> BuiltGraph:
    files = sorted(sources)
    cfg = make_config()
    test_files = frozenset(discover_test_files(files, cfg))
    kinds = {path: classify_file(path, cfg, test_files) for path in files}
    facts = {path: parse_module_facts(text.encode(), path) for path, text in sources.items()}
    conftest_facts = {
        path: inspect_conftest(text.encode(), path)
        for path, text in sources.items()
        if path.rsplit("/", 1)[-1] == "conftest.py"
    }
    return assemble_graph(
        files=files,
        kinds=kinds,
        facts=facts,
        unparseable=(),
        index=build_index(files, roots),
        conftest_facts=conftest_facts,
        pytest_config=cfg,
    )


def edge_set(graph: BuiltGraph) -> set[tuple[str, str, EdgeKind]]:
    dg = graph.digraph
    return {(dg[u].path, dg[v].path, kind) for u, v, kind in dg.weighted_edge_list()}


def test_pure_init_outgoing_edges_are_annotated() -> None:
    edges = edge_set(assemble(SOURCES))
    assert ("pkg/__init__.py", "pkg/table.py", EdgeKind.INIT_REEXPORT) in edges
    assert ("pkg/__init__.py", "pkg/console.py", EdgeKind.INIT_REEXPORT) in edges
    # The relative import's prefix edge lands on the init itself.
    assert ("pkg/__init__.py", "pkg/__init__.py", EdgeKind.INIT_REEXPORT) in edges


def test_pure_init_keeps_no_plain_import_edges() -> None:
    edges = edge_set(assemble(SOURCES))
    assert not any(kind is EdgeKind.IMPORTS for src, _, kind in edges if src == "pkg/__init__.py")


def test_consumer_gains_a_full_edge_to_the_symbol_home() -> None:
    edges = edge_set(assemble(SOURCES))
    assert ("tests/test_table.py", "pkg/__init__.py", EdgeKind.IMPORTS) in edges
    assert ("tests/test_table.py", "pkg/table.py", EdgeKind.IMPORTS) in edges
    # The non-home sibling stays reachable only through the init.
    assert ("tests/test_table.py", "pkg/console.py", EdgeKind.IMPORTS) not in edges


def test_proven_inits_are_recorded_with_their_tier() -> None:
    graph = assemble(SOURCES)
    assert graph.reexport_inits == {"pkg/__init__.py": ReexportTier.STRICT}


def test_closure_still_reaches_the_sibling_through_the_init() -> None:
    # Wave one is inert by construction: INIT_REEXPORT edges participate in
    # reachability, so the sibling stays inside the consumer's closure.
    from acquit.select import import_closure

    graph = assemble(SOURCES)
    closure = import_closure(graph, "tests/test_table.py")
    assert "pkg/console.py" in closure
    assert "pkg/table.py" in closure


def test_attribution_chases_through_chains_of_pure_inits() -> None:
    sources = {
        "pkg/__init__.py": "from .sub import Deep\n",
        "pkg/sub/__init__.py": "from .impl import Deep\n",
        "pkg/sub/impl.py": "class Deep: ...\n",
        "tests/test_deep.py": "from pkg import Deep\n",
    }
    edges = edge_set(assemble(sources))
    # Every init on the chain gets a full edge; so does the ultimate home.
    assert ("tests/test_deep.py", "pkg/sub/__init__.py", EdgeKind.IMPORTS) in edges
    assert ("tests/test_deep.py", "pkg/sub/impl.py", EdgeKind.IMPORTS) in edges


def test_chase_stops_at_an_impure_init_whose_edges_are_full() -> None:
    sources = {
        "pkg/__init__.py": "from .sub import Deep\n",
        "pkg/sub/__init__.py": "from .impl import Deep\ndef helper(): ...\n",
        "pkg/sub/impl.py": "class Deep: ...\n",
        "tests/test_deep.py": "from pkg import Deep\n",
    }
    graph = assemble(sources)
    edges = edge_set(graph)
    assert graph.reexport_inits == {"pkg/__init__.py": ReexportTier.STRICT}
    # The impure init gets the full chain edge and keeps full edges of its
    # own, so transitivity covers impl.py without a direct edge.
    assert ("tests/test_deep.py", "pkg/sub/__init__.py", EdgeKind.IMPORTS) in edges
    assert ("tests/test_deep.py", "pkg/sub/impl.py", EdgeKind.IMPORTS) not in edges
    assert ("pkg/sub/__init__.py", "pkg/sub/impl.py", EdgeKind.IMPORTS) in edges


def test_module_valued_name_fans_out_its_pure_init() -> None:
    sources = {
        "pkg/__init__.py": "from . import helpers\n",
        "pkg/helpers/__init__.py": "from .fmt import fmt\n",
        "pkg/helpers/fmt.py": "def fmt(): ...\n",
        "tests/test_helpers.py": "from pkg import helpers\n",
    }
    edges = edge_set(assemble(sources))
    # helpers is a module object; attribute uses on it are invisible, so the
    # consumer takes the full fan-out of the helpers init.
    assert ("tests/test_helpers.py", "pkg/helpers/__init__.py", EdgeKind.IMPORTS) in edges
    assert ("tests/test_helpers.py", "pkg/helpers/fmt.py", EdgeKind.IMPORTS) in edges


def test_unattributable_name_takes_the_full_fanout() -> None:
    sources = dict(SOURCES) | {"tests/test_table.py": "from pkg import mystery\n"}
    edges = edge_set(assemble(sources))
    assert ("tests/test_table.py", "pkg/table.py", EdgeKind.IMPORTS) in edges
    assert ("tests/test_table.py", "pkg/console.py", EdgeKind.IMPORTS) in edges


def test_plain_import_consumer_takes_the_full_fanout() -> None:
    sources = dict(SOURCES) | {"tests/test_table.py": "import pkg\n"}
    edges = edge_set(assemble(sources))
    assert ("tests/test_table.py", "pkg/table.py", EdgeKind.IMPORTS) in edges
    assert ("tests/test_table.py", "pkg/console.py", EdgeKind.IMPORTS) in edges


def test_dynamic_import_consumer_fans_out_with_the_dynamic_kind() -> None:
    sources = dict(SOURCES) | {
        "tests/test_table.py": "import importlib\nimportlib.import_module('pkg')\n"
    }
    edges = edge_set(assemble(sources))
    assert ("tests/test_table.py", "pkg/table.py", EdgeKind.DYNAMIC_IMPORT) in edges
    assert ("tests/test_table.py", "pkg/console.py", EdgeKind.DYNAMIC_IMPORT) in edges


def test_star_import_consumer_takes_the_full_fanout() -> None:
    sources = dict(SOURCES) | {"tests/test_table.py": "from pkg import *\n"}
    edges = edge_set(assemble(sources))
    assert ("tests/test_table.py", "pkg/table.py", EdgeKind.IMPORTS) in edges
    assert ("tests/test_table.py", "pkg/console.py", EdgeKind.IMPORTS) in edges
    # The existing star expansion stays untouched alongside.
    assert ("tests/test_table.py", "pkg/table.py", EdgeKind.STAR_IMPORT) in edges


def test_star_tier_init_annotates_and_attributes_through_all() -> None:
    sources = {
        "pkg/__init__.py": "from ._impl import *\n",
        "pkg/_impl.py": "__all__ = ['alpha']\ndef alpha(): ...\ndef _hidden(): ...\n",
        "tests/test_alpha.py": "from pkg import alpha\n",
    }
    graph = assemble(sources)
    edges = edge_set(graph)
    assert graph.reexport_inits == {"pkg/__init__.py": ReexportTier.STAR_ALL}
    assert ("pkg/__init__.py", "pkg/_impl.py", EdgeKind.INIT_REEXPORT) in edges
    assert ("tests/test_alpha.py", "pkg/_impl.py", EdgeKind.IMPORTS) in edges


def test_star_source_without_a_literal_all_declines_everything() -> None:
    sources = {
        "pkg/__init__.py": "from ._impl import *\n",
        "pkg/_impl.py": "def alpha(): ...\n",
        "tests/test_alpha.py": "from pkg import alpha\n",
    }
    graph = assemble(sources)
    assert graph.reexport_inits == {}
    assert not any(kind is EdgeKind.INIT_REEXPORT for _, _, kind in edge_set(graph))


def test_pure_init_consuming_another_gains_no_full_edges() -> None:
    sources = {
        "pkg/__init__.py": "from .sub import Deep\n",
        "pkg/sub/__init__.py": "from .impl import Deep\n",
        "pkg/sub/impl.py": "class Deep: ...\n",
        "tests/test_deep.py": "from pkg import Deep\n",
    }
    edges = edge_set(assemble(sources))
    outgoing = {(dst, kind) for src, dst, kind in edges if src == "pkg/__init__.py"}
    # All of the init's own edges stay import-time-only; the consumer-side
    # chase is what pins deep homes, never the init itself.
    assert outgoing == {
        ("pkg/__init__.py", EdgeKind.INIT_REEXPORT),
        ("pkg/sub/__init__.py", EdgeKind.INIT_REEXPORT),
    }


def test_init_with_module_getattr_declines() -> None:
    sources = dict(SOURCES) | {
        "pkg/__init__.py": "from .table import Table\ndef __getattr__(name): ...\n"
    }
    graph = assemble(sources)
    assert graph.reexport_inits == {}
    assert not any(kind is EdgeKind.INIT_REEXPORT for _, _, kind in edge_set(graph))


def test_init_with_a_broken_import_declines() -> None:
    sources = dict(SOURCES) | {"pkg/__init__.py": "from .table import Table\nimport pkg.gone\n"}
    graph = assemble(sources)
    assert graph.reexport_inits == {}
    assert graph.nodes["pkg/__init__.py"].tainted


def test_docstring_only_init_proves_trivially_and_changes_nothing() -> None:
    sources = {
        "pkg/__init__.py": '"""pkg"""\n',
        "pkg/util.py": "def helper(): ...\n",
        "tests/test_util.py": "from pkg import util\n",
    }
    graph = assemble(sources)
    assert graph.reexport_inits == {"pkg/__init__.py": ReexportTier.STRICT}
    assert edge_set(graph) == {
        ("tests/test_util.py", "pkg/__init__.py", EdgeKind.IMPORTS),
        ("tests/test_util.py", "pkg/util.py", EdgeKind.IMPORTS),
    }


def test_plugin_declaration_through_a_pure_init_fans_out() -> None:
    sources = dict(SOURCES) | {"conftest.py": "pytest_plugins = ['pkg']\n"}
    edges = edge_set(assemble(sources))
    assert ("conftest.py", "pkg/__init__.py", EdgeKind.PLUGIN) in edges
    assert ("conftest.py", "pkg/table.py", EdgeKind.PLUGIN) in edges
    assert ("conftest.py", "pkg/console.py", EdgeKind.PLUGIN) in edges


def test_impure_init_graph_is_byte_identical_to_the_pre_narrowing_shape() -> None:
    # The exact edge set the builder produced before this feature existed;
    # nothing qualifies, so nothing may differ, kinds included.
    sources = dict(SOURCES) | {"pkg/__init__.py": PURE_INIT + "def impure(): ...\n"}
    graph = assemble(sources)
    assert graph.reexport_inits == {}
    assert edge_set(graph) == {
        ("pkg/__init__.py", "pkg/__init__.py", EdgeKind.IMPORTS),
        ("pkg/__init__.py", "pkg/console.py", EdgeKind.IMPORTS),
        ("pkg/__init__.py", "pkg/table.py", EdgeKind.IMPORTS),
        ("tests/test_table.py", "pkg/__init__.py", EdgeKind.IMPORTS),
    }


def test_assembly_with_narrowing_is_deterministic() -> None:
    first = assemble(SOURCES)
    second = assemble(dict(reversed(list(SOURCES.items()))))
    assert first.graph_hash == second.graph_hash
    assert list(first.digraph.weighted_edge_list()) == list(second.digraph.weighted_edge_list())
    assert first.reexport_inits == second.reexport_inits
