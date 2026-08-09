"""Unit tests for single-pass module fact extraction."""

import pytest

from acquit.errors import GraphError, ParseFailure
from acquit.graph.parse import ImportStmt, ModuleFacts, Suspect, SuspectKind, parse_module_facts
from acquit.graph.resolvers.checkers import ReexportScan
from acquit.graph.resolvers.folding import AnchoredName


def facts(source: str) -> ModuleFacts:
    return parse_module_facts(source.encode("utf-8"), "pkg/mod.py")


def test_plain_import() -> None:
    result = facts("import a.b")
    assert result.imports == (ImportStmt(module=None, names=("a.b",), level=0, is_star=False),)


def test_plain_import_multiple_names() -> None:
    result = facts("import a.b, c")
    assert result.imports == (ImportStmt(module=None, names=("a.b", "c"), level=0, is_star=False),)


def test_from_import_absolute() -> None:
    result = facts("from a.b import c, d")
    assert result.imports == (ImportStmt(module="a.b", names=("c", "d"), level=0, is_star=False),)


def test_aliases_keep_original_names() -> None:
    result = facts("import x.y as z\nfrom a import b as c")
    assert result.imports == (
        ImportStmt(module=None, names=("x.y",), level=0, is_star=False),
        ImportStmt(module="a", names=("b",), level=0, is_star=False),
    )


def test_relative_import_levels() -> None:
    result = facts("from . import x\nfrom ..m import y")
    assert result.imports == (
        ImportStmt(module="", names=("x",), level=1, is_star=False),
        ImportStmt(module="m", names=("y",), level=2, is_star=False),
    )


def test_star_import() -> None:
    result = facts("from a.b import *")
    assert result.imports == (ImportStmt(module="a.b", names=("*",), level=0, is_star=True),)


def test_conditional_imports_collected_unconditionally() -> None:
    source = """\
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import typed_only

try:
    import maybe
except ImportError:
    maybe = None


def late() -> None:
    import lazy
"""
    result = facts(source)
    modules = [stmt.names[0] for stmt in result.imports if stmt.module is None]
    assert modules == ["typed_only", "maybe", "lazy"]


def test_dynamic_import_literal_import_module() -> None:
    result = facts('import importlib\nimportlib.import_module("a.b")')
    assert result.dyn_literal_imports == ("a.b",)
    assert result.suspects == ()


def test_dynamic_import_literal_dunder_import() -> None:
    result = facts('__import__("x")')
    assert result.dyn_literal_imports == ("x",)
    assert result.suspects == ()


def test_dynamic_import_literal_bare_import_module() -> None:
    result = facts('from importlib import import_module\nimport_module("y")')
    assert result.dyn_literal_imports == ("y",)


def test_dynamic_import_relative_with_literal_package_resolves() -> None:
    result = facts("import importlib\nimportlib.import_module('.helper', 'pkg')")
    assert result.dyn_literal_imports == ("pkg.helper",)
    assert result.suspects == ()


def test_dynamic_import_relative_with_package_keyword_resolves() -> None:
    result = facts("import importlib\nimportlib.import_module('..x', package='pkg.sub')")
    assert result.dyn_literal_imports == ("pkg.x",)
    assert result.suspects == ()


def test_dynamic_import_relative_without_package_is_suspect() -> None:
    result = facts("import importlib\nimportlib.import_module('.helper')")
    assert result.dyn_literal_imports == ()
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=2),)


def test_dynamic_import_relative_with_non_literal_package_is_suspect() -> None:
    result = facts("import importlib\nimportlib.import_module('.helper', anchor)")
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=2),)


def test_dynamic_import_relative_beyond_package_is_suspect() -> None:
    result = facts("import importlib\nimportlib.import_module('..x', 'pkg')")
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=2),)


def test_dunder_import_relative_is_suspect() -> None:
    # __import__'s second argument is globals, never a package anchor.
    result = facts("__import__('.helper', {'__package__': 'pkg'})")
    assert result.dyn_literal_imports == ()
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=1),)


def test_dynamic_import_non_literal_is_suspect() -> None:
    result = facts("import importlib\nname = compute()\nimportlib.import_module(name)")
    assert result.dyn_literal_imports == ()
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=3),)


def test_dynamic_import_without_positional_arg_is_suspect() -> None:
    result = facts("import importlib\nimportlib.import_module(name=target)")
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=2),)


def test_sys_path_method_calls_at_module_level_are_import_time() -> None:
    source = """\
import sys

sys.path.append("a")
sys.path.insert(0, "b")
sys.path.extend(["c"])
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION_IMPORT_TIME] * 3
    assert [s.lineno for s in result.suspects] == [3, 4, 5]


def test_sys_path_method_calls_in_function_body_are_runtime() -> None:
    source = """\
import sys


def vendor():
    sys.path.append("a")
    sys.path.insert(0, "b")
    sys.path.extend(["c"])
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION] * 3


def test_sys_path_assignment_forms_at_module_level_are_import_time() -> None:
    source = """\
import sys

sys.path = ["a"]
sys.path += ["b"]
sys.path[:0] = ["c"]
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION_IMPORT_TIME] * 3


def test_sys_path_assignment_forms_in_function_body_are_runtime() -> None:
    source = """\
import sys


def vendor():
    sys.path = ["a"]
    sys.path += ["b"]
    sys.path[:0] = ["c"]
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION] * 3


def test_sys_path_mutation_in_class_body_is_import_time() -> None:
    # Class bodies execute when the module is imported.
    source = """\
import sys


class Vendored:
    sys.path.append("a")
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION_IMPORT_TIME]


def test_sys_path_mutation_in_method_body_is_runtime() -> None:
    source = """\
import sys


class Vendored:
    def vendor(self):
        sys.path.append("a")
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION]


def test_sys_path_mutation_in_class_inside_function_is_runtime() -> None:
    # The class statement itself only executes when the function is called.
    source = """\
import sys


def build():
    class Vendored:
        sys.path.append("a")
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION]


def test_sys_path_mutation_in_async_function_is_runtime() -> None:
    source = """\
import sys


async def vendor():
    sys.path.append("a")
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION]


def test_path_dunder_assignment() -> None:
    result = facts("__path__ = extend()")
    assert result.suspects == (Suspect(kind=SuspectKind.SYS_PATH_MUTATION_IMPORT_TIME, lineno=1),)


def test_path_dunder_assignment_in_function_is_runtime() -> None:
    result = facts("def rewire():\n    __path__ = extend()")
    assert result.suspects == (Suspect(kind=SuspectKind.SYS_PATH_MUTATION, lineno=2),)


def test_site_addsitedir_and_pkgutil_extend_path_at_module_level() -> None:
    source = """\
import pkgutil
import site

site.addsitedir("vendored")
pkgutil.extend_path(p, n)
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION_IMPORT_TIME] * 2


def test_site_addsitedir_and_pkgutil_extend_path_in_function() -> None:
    source = """\
import pkgutil
import site


def vendor():
    site.addsitedir("vendored")
    pkgutil.extend_path(p, n)
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION] * 2


def test_monkeypatch_syspath_prepend_in_fixture_body_is_runtime() -> None:
    source = """\
import pytest


@pytest.fixture
def test_apps(monkeypatch):
    monkeypatch.syspath_prepend("vendored")
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION]


def test_pytester_syspathinsert_in_function_body_is_runtime() -> None:
    source = """\
def test_plugin(pytester):
    pytester.syspathinsert("vendored")
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION]


def test_monkeypatch_syspath_prepend_at_module_level_is_import_time() -> None:
    # Contrived, but a MonkeyPatch instance can be built and used anywhere.
    source = """\
from _pytest.monkeypatch import MonkeyPatch

mp = MonkeyPatch()
mp.syspath_prepend("vendored")
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION_IMPORT_TIME]


def test_syspath_prepend_on_arbitrary_receiver_still_counts() -> None:
    # Receiver types are unknowable statically; over-approximating is sound.
    result = facts("def use(thing):\n    thing.syspath_prepend('x')")
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION]


def test_function_named_syspath_prepend_defined_but_not_called_is_silent() -> None:
    result = facts("def syspath_prepend(path):\n    pass")
    assert result.suspects == ()


def test_exec_eval_compile() -> None:
    source = 'exec(code)\neval(expr)\ncompile(src, "<s>", "exec")'
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.EXEC_EVAL] * 3


def test_method_named_eval_is_not_suspect() -> None:
    result = facts('model.eval("x")\nsession.compile(y)')
    assert result.suspects == ()


def test_suspects_collected_inside_functions() -> None:
    result = facts("def run() -> None:\n    exec(code)")
    assert result.suspects == (Suspect(kind=SuspectKind.EXEC_EVAL, lineno=2),)


def test_module_getattr_def_with_static_body_is_not_a_suspect() -> None:
    source = "def __getattr__(name):\n    from pkg.core import thing\n    return thing"
    result = facts(source)
    assert result.defines_module_getattr is True
    assert result.suspects == ()
    assert result.imports == (
        ImportStmt(module="pkg.core", names=("thing",), level=0, is_star=False),
    )


def test_module_getattr_def_with_dynamic_body_taints_via_the_body() -> None:
    source = "import importlib\n\ndef __getattr__(name):\n    return importlib.import_module(name)"
    result = facts(source)
    assert result.defines_module_getattr is True
    kinds = {suspect.kind for suspect in result.suspects}
    assert kinds == {SuspectKind.NON_LITERAL_DYNAMIC_IMPORT}


def test_module_getattr_assignment() -> None:
    result = facts("__getattr__ = make_lazy_hook()")
    assert result.defines_module_getattr is True
    assert result.suspects == (Suspect(kind=SuspectKind.LAZY_MODULE_GETATTR, lineno=1),)


def test_sys_modules_literal_subscript_is_a_literal_dynamic_import() -> None:
    result = facts("import sys\nmod = sys.modules['pkg.core']")
    assert result.dyn_literal_imports == ("pkg.core",)
    assert result.suspects == ()


def test_sys_modules_non_literal_subscript_is_a_suspect() -> None:
    result = facts("import sys\nmod = sys.modules[name]")
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=2),)


def test_sys_modules_relative_key_is_a_suspect() -> None:
    result = facts("import sys\nmod = sys.modules['.helper']")
    assert result.dyn_literal_imports == ()
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=2),)


def test_sys_modules_get_with_variable_is_a_suspect() -> None:
    result = facts("import sys\nmod = sys.modules.get(name)")
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=2),)


def test_folded_dynamic_import_is_not_a_suspect() -> None:
    # ADR 0009: a proven site records a fold instead of tainting.
    source = "from importlib import import_module\nfor n in ('a.x', 'a.y'):\n    import_module(n)\n"
    result = facts(source)
    assert result.suspects == ()
    (fold,) = result.folded_dynamic_imports
    assert fold.lineno == 3
    assert fold.names == ("a.x", "a.y")
    assert fold.anchored == ()
    assert result.dyn_literal_imports == ()


def test_declined_dynamic_import_keeps_its_suspect() -> None:
    result = facts(
        "from importlib import import_module\ndef load(n):\n    return import_module(n)\n"
    )
    assert result.folded_dynamic_imports == ()
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=3),)


def test_mixed_module_keeps_fold_and_suspect_apart() -> None:
    source = (
        "from importlib import import_module\n"
        "MOD = 'pkg.a'\n"
        "import_module(MOD)\n"
        "import_module('pkg.lit')\n"
        "def load(name):\n"
        "    return import_module(name)\n"
    )
    result = facts(source)
    assert result.dyn_literal_imports == ("pkg.lit",)
    assert [(fold.lineno, fold.names) for fold in result.folded_dynamic_imports] == [
        (3, ("pkg.a",))
    ]
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=6),)


def test_sys_modules_dunder_name_folds_to_an_anchor() -> None:
    result = facts("import sys\nmod = sys.modules[__name__]\n")
    assert result.suspects == ()
    (fold,) = result.folded_dynamic_imports
    assert fold.names == ()
    assert fold.anchored == (AnchoredName(anchor="__name__", ascend=0, suffix=""),)


def test_sys_modules_in_annotation_is_still_detected() -> None:
    # The folder's walk must reach every expression the old visitor did.
    result = facts("import sys\ndef f(x: sys.modules[key]) -> None:\n    pass\n")
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=2),)


def test_facts_are_deterministic_for_folded_modules() -> None:
    source = (
        "import sys\n"
        "from importlib import import_module\n"
        "for n in ('pkg.a', 'pkg.b'):\n"
        "    import_module(n)\n"
        "mod = sys.modules[__name__]\n"
        "def load(name):\n"
        "    return import_module(name)\n"
    )
    assert facts(source) == facts(source)


def test_nested_getattr_is_not_module_level() -> None:
    source = """\
class Proxy:
    def __getattr__(self, name):
        return None


def outer():
    def __getattr__(name):
        return None
"""
    result = facts(source)
    assert result.defines_module_getattr is False
    assert result.suspects == ()


def test_pytest_plugins_list() -> None:
    result = facts('pytest_plugins = ["a.b", "c"]')
    assert result.pytest_plugins_decl == ("a.b", "c")


def test_pytest_plugins_single_string() -> None:
    result = facts('pytest_plugins = "a.b"')
    assert result.pytest_plugins_decl == ("a.b",)


def test_pytest_plugins_tuple_skips_non_literals() -> None:
    result = facts('pytest_plugins = ("a", computed)')
    assert result.pytest_plugins_decl == ("a",)


def test_pytest_plugins_not_module_level_ignored() -> None:
    result = facts('def setup():\n    pytest_plugins = ["a"]')
    assert result.pytest_plugins_decl == ()


def test_parse_failure_on_syntax_error() -> None:
    with pytest.raises(ParseFailure) as excinfo:
        parse_module_facts(b"def (:", "bad/file.py")
    assert isinstance(excinfo.value, GraphError)
    assert "bad/file.py" in str(excinfo.value)


def test_parse_failure_on_null_byte() -> None:
    with pytest.raises(ParseFailure):
        parse_module_facts(b"x = 1\x00", "bad/null.py")


def test_empty_module() -> None:
    result = facts("")
    assert result == ModuleFacts(
        path="pkg/mod.py",
        imports=(),
        dyn_literal_imports=(),
        folded_dynamic_imports=(),
        suspects=(),
        defines_module_getattr=False,
        pytest_plugins_decl=(),
        reexport=ReexportScan(reason=None, bindings=(), stars=(), local_names=(), all_names=None),
        inert_reason=None,
        bound_names=(),
    )
