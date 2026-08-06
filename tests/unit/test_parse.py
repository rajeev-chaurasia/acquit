"""Unit tests for single-pass module fact extraction."""

import pytest

from acquit.errors import GraphError, ParseFailure
from acquit.graph.parse import ImportStmt, ModuleFacts, Suspect, SuspectKind, parse_module_facts


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


def test_dynamic_import_non_literal_is_suspect() -> None:
    result = facts("import importlib\nname = compute()\nimportlib.import_module(name)")
    assert result.dyn_literal_imports == ()
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=3),)


def test_dynamic_import_without_positional_arg_is_suspect() -> None:
    result = facts("import importlib\nimportlib.import_module(name=target)")
    assert result.suspects == (Suspect(kind=SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, lineno=2),)


def test_sys_path_method_calls() -> None:
    source = """\
import sys

sys.path.append("a")
sys.path.insert(0, "b")
sys.path.extend(["c"])
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION] * 3
    assert [s.lineno for s in result.suspects] == [3, 4, 5]


def test_sys_path_assignment_forms() -> None:
    source = """\
import sys

sys.path = ["a"]
sys.path += ["b"]
sys.path[:0] = ["c"]
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION] * 3


def test_path_dunder_assignment() -> None:
    result = facts("__path__ = extend()")
    assert result.suspects == (Suspect(kind=SuspectKind.SYS_PATH_MUTATION, lineno=1),)


def test_site_addsitedir_and_pkgutil_extend_path() -> None:
    source = """\
import pkgutil
import site

site.addsitedir("vendored")
pkgutil.extend_path(p, n)
"""
    result = facts(source)
    assert [s.kind for s in result.suspects] == [SuspectKind.SYS_PATH_MUTATION] * 2


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


def test_module_getattr_def() -> None:
    result = facts("def __getattr__(name):\n    return lazy(name)")
    assert result.defines_module_getattr is True
    assert result.suspects == (Suspect(kind=SuspectKind.LAZY_MODULE_GETATTR, lineno=1),)


def test_module_getattr_assignment() -> None:
    result = facts("__getattr__ = make_lazy_hook()")
    assert result.defines_module_getattr is True
    assert result.suspects == (Suspect(kind=SuspectKind.LAZY_MODULE_GETATTR, lineno=1),)


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
        suspects=(),
        defines_module_getattr=False,
        pytest_plugins_decl=(),
    )
