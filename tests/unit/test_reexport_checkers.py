"""Unit tests for the re-export narrowing checkers (ADR 0008).

The adversarial gallery below is the design document's counterexample list,
ported case for case from the prototype that validated the whitelists: every
idiom the whitelist must reject, with the reason, and every idiom it must
admit. The gallery is the contract; loosening a checker should break here
first.
"""

import ast

import pytest

from acquit.graph.resolvers.checkers import (
    BindingForm,
    ReexportBinding,
    ReexportTier,
    bound_name_set,
    module_inertness,
    pure_reexporter,
    scan_reexports,
)


def parse(source: str) -> ast.Module:
    return ast.parse(source)


# Each entry: (case, source, reject reason). One line each on why it rejects.
INERT_MUST_REJECT = [
    # EnumMeta machinery runs at import; Enum is an imported base.
    ("enum", "from enum import Enum\nclass Color(Enum):\n    RED = 1\n", "nonlocal-base"),
    # A decorator is a call at import time.
    (
        "dataclass",
        "from dataclasses import dataclass\n@dataclass\nclass P:\n    x: int = 0\n",
        "class-decorator",
    ),
    # attr.s is a decorator too, and attr.ib() would also be a call.
    ("attrs", "import attr\n@attr.s\nclass P:\n    x = attr.ib()\n", "class-decorator"),
    # namedtuple() executes at import.
    (
        "namedtuple",
        "from collections import namedtuple\nP = namedtuple('P', 'x y')\n",
        "non-inert-value",
    ),
    # TypeVar() executes at import.
    ("typevar", "from typing import TypeVar\nT = TypeVar('T')\n", "non-inert-value"),
    # The sharpest counterexample: an imported base can run __init_subclass__.
    (
        "init-subclass-registry",
        "from pkg.base import RendererBase\n"
        "class ConsoleRenderer(RendererBase):\n    def render(self): ...\n",
        "nonlocal-base",
    ),
    # Binding a name into a class namespace can invoke __set_name__.
    (
        "set-name-descriptor",
        "from pkg.desc import tracked\nclass C:\n    x = tracked\n",
        "class-body-value",
    ),
    # Default parameter values evaluate at import.
    ("evaluated-default", "def f(x=compute()): ...\n", "evaluated-default"),
    # Attribute access can run a module __getattr__ or a property.
    ("dotted-default", "import config\ndef f(x=config.DEFAULT): ...\n", "evaluated-default"),
    # Annotations evaluate at definition time without the future import.
    ("evaluated-annotation", "import t\ndef f(x: t.Thing): ...\n", "evaluated-annotation"),
    # A module __getattr__ makes attribute access run code a diff could change.
    ("module-getattr-def", "def __getattr__(name): ...\n", "module-hook-def"),
    # The assignment form of the same hook.
    ("module-getattr-assign", "__getattr__ = lambda name: 1\n", "module-hook-assignment"),
    # getLogger mutates the process-global logging manager.
    (
        "call-at-module-level",
        "import logging\nlog = logging.getLogger(__name__)\n",
        "non-inert-value",
    ),
    # Any conditional outside a TYPE_CHECKING guard rejects.
    (
        "conditional-version",
        "import sys\nif sys.version_info >= (3, 11):\n    import tomllib\n",
        "conditional",
    ),
    # Star evaluates __all__ on the source, or walks its module dict.
    ("star-import", "from pkg.other import *\n", "star-import"),
    # metaclass= is arbitrary code at class creation.
    (
        "class-keywords",
        "from pkg.base import Meta\nclass C(metaclass=Meta): ...\n",
        "class-keywords",
    ),
    # Loops execute at import.
    ("loop", "for i in range(3):\n    pass\n", "for"),
    # del unbinds at import.
    ("del", "import os\ndel os\n", "delete"),
    # Arguing import context would couple the checker to how the suite runs.
    ("main-guard", "if __name__ == '__main__':\n    print('hi')\n", "conditional"),
    # Augmented assignment invokes __iadd__ on the current value.
    ("aug-all", "__all__ = ['a']\n__all__ += ['b']\n", "augassign"),
    # A walrus is not in the inert expression grammar.
    ("walrus", "X = (y := 5)\n", "non-inert-value"),
    # Formatting a name invokes __format__.
    ("fstring-of-name", "import pkg\nV = f'{pkg}'\n", "non-inert-value"),
    # Class-body values are constants only, even names inert at module level.
    ("class-body-name-value", "import pkg\nclass C:\n    x = pkg\n", "class-body-value"),
    # The shadowing assignment itself rejects (t = object() is a call).
    (
        "typing-shadowed",
        "import typing as t\nt = object()\nif t.TYPE_CHECKING:\n    import pkg\n",
        "non-inert-value",
    ),
]

# Each entry: (case, source). One line each on why it is admissible.
INERT_MUST_ACCEPT = [
    # Docstring, imports, constant, def with inert default: the whole whitelist.
    (
        "docstring-imports-defs",
        '"""doc"""\nfrom __future__ import annotations\nimport os\n'
        "from pkg import helper\nX = 1\ndef f(a, b=1): return helper(a)\n",
    ),
    # t.TYPE_CHECKING with t bound once by import typing as t.
    ("t-type-checking", "import typing as t\nif t.TYPE_CHECKING:\n    from pkg import Thing\n"),
    # Local hookless bases are provable.
    ("local-base-chain", "class Base:\n    def m(self): ...\nclass Child(Base):\n    X = 1\n"),
    # __slots__ with a literal tuple: type() consumes it without user code.
    ("slots-literals", "class C:\n    __slots__ = ('a', 'b')\n    def m(self): ...\n"),
    # The compat idiom: whichever branch runs, the effect is imports plus bindings.
    (
        "try-import-fallback",
        "try:\n    import ujson\nexcept ImportError:\n    import json as ujson\n",
    ),
    # Future annotations turn every annotation into a non-evaluated string.
    (
        "future-annotations-any",
        "from __future__ import annotations\nimport pkg\n"
        "def f(x: pkg.Weird[int]) -> pkg.Also: ...\n",
    ),
]

# Each entry: (case, source, reject reason) against the pure re-exporter check.
INIT_MUST_REJECT = [
    # An init that defines behavior is not a manifest.
    ("init-def", "from .a import x\ndef helper(): ...\n", "def"),
    # try/except ImportError makes the bound-name set environment-dependent.
    (
        "init-conditional-import",
        "try:\n    from ._main import main\nexcept ImportError:\n    pass\n",
        "conditional-import",
    ),
    # PEP 562 lazy inits make the binding set dynamic.
    ("init-getattr", "def __getattr__(name): ...\n", "def"),
    # Stars reject on the strict tier; tier two needs a resolvable source.
    ("init-star", "from ._api import *\n", "star-import"),
    # Any call expression disqualifies the whole init.
    ("init-call", "from .a import setup\nsetup()\n", "expression"),
]

# Each entry: (case, source, proven tier).
INIT_MUST_ACCEPT = [
    # The flask shape: docstring, re-exports, literal metadata, literal __all__.
    (
        "flask-style",
        '"""pkg"""\nfrom .app import App as App\nfrom . import helpers as helpers\n'
        "__version__ = '1.0'\n__all__ = ['App', 'helpers']\n",
        ReexportTier.STRICT,
    ),
]


@pytest.mark.parametrize(
    ("case", "source", "reason"), INERT_MUST_REJECT, ids=[c[0] for c in INERT_MUST_REJECT]
)
def test_inertness_rejects(case: str, source: str, reason: str) -> None:
    verdict = module_inertness(parse(source))
    assert not verdict.ok, case
    assert verdict.reason == reason


@pytest.mark.parametrize(
    ("case", "source"), INERT_MUST_ACCEPT, ids=[c[0] for c in INERT_MUST_ACCEPT]
)
def test_inertness_accepts(case: str, source: str) -> None:
    verdict = module_inertness(parse(source))
    assert verdict.ok, f"{case}: {verdict.reason}"


@pytest.mark.parametrize(
    ("case", "source", "reason"), INIT_MUST_REJECT, ids=[c[0] for c in INIT_MUST_REJECT]
)
def test_pure_reexporter_rejects(case: str, source: str, reason: str) -> None:
    verdict = pure_reexporter(parse(source))
    assert verdict.tier is None, case
    assert verdict.reason == reason


@pytest.mark.parametrize(
    ("case", "source", "tier"), INIT_MUST_ACCEPT, ids=[c[0] for c in INIT_MUST_ACCEPT]
)
def test_pure_reexporter_accepts(case: str, source: str, tier: ReexportTier) -> None:
    verdict = pure_reexporter(parse(source))
    assert verdict.tier is tier, verdict.reason


# ---------------------------------------------------------------------------
# TYPE_CHECKING guard shadowing, beyond the gallery
# ---------------------------------------------------------------------------


def test_inertly_shadowed_typing_alias_still_rejects_the_guard() -> None:
    # t = 1 passes the assignment whitelist, so the guard check must catch it.
    source = "import typing as t\nt = 1\nif t.TYPE_CHECKING:\n    import pkg\n"
    assert module_inertness(parse(source)).reason == "conditional"


def test_rebound_bare_type_checking_rejects_the_guard() -> None:
    source = "TYPE_CHECKING = 1\nif TYPE_CHECKING:\n    import pkg\n"
    assert module_inertness(parse(source)).reason == "conditional"


def test_type_checking_imported_from_elsewhere_rejects_the_guard() -> None:
    source = "from mypkg import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pkg\n"
    assert module_inertness(parse(source)).reason == "conditional"


def test_type_checking_else_branch_is_checked_because_it_runs() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    import pkg\nelse:\n    x = compute()\n"
    )
    assert module_inertness(parse(source)).reason == "non-inert-value"


def test_bare_type_checking_without_any_import_is_trusted() -> None:
    source = "if TYPE_CHECKING:\n    import pkg\n"
    assert module_inertness(parse(source)).ok


# ---------------------------------------------------------------------------
# Inertness whitelist edges, beyond the gallery
# ---------------------------------------------------------------------------


def test_tuple_unpack_from_literal_display_is_inert() -> None:
    assert module_inertness(parse("a, b = 1, 2\n")).ok


def test_tuple_unpack_from_a_call_rejects() -> None:
    assert module_inertness(parse("a, b = pair()\n")).reason == "unpack-from-non-display"


def test_tuple_unpack_from_a_name_rejects_as_user_iteration() -> None:
    assert module_inertness(parse("import x\na, b = x\n")).reason == "unpack-from-non-display"


def test_set_display_with_a_name_element_rejects() -> None:
    # Set elements hash at construction.
    assert module_inertness(parse("import x\nS = {x}\n")).reason == "non-inert-value"


def test_dict_with_a_name_key_rejects() -> None:
    assert module_inertness(parse("import x\nD = {x: 1}\n")).reason == "non-inert-value"


def test_operators_on_names_reject() -> None:
    assert module_inertness(parse("A = 1\nB = A + 1\n")).reason == "non-inert-value"


def test_lambda_with_inert_defaults_is_inert_at_module_level() -> None:
    assert module_inertness(parse("f = lambda x, y=1: x + y\n")).ok


def test_lambda_with_evaluated_default_rejects() -> None:
    assert module_inertness(parse("f = lambda x=compute(): x\n")).reason == "non-inert-value"


def test_path_assignment_rejects_as_a_module_hook() -> None:
    assert module_inertness(parse("__path__ = ['x']\n")).reason == "module-hook-assignment"


def test_dir_def_rejects_as_a_module_hook() -> None:
    assert module_inertness(parse("def __dir__(): ...\n")).reason == "module-hook-def"


def test_rebound_hookless_base_is_evicted() -> None:
    # After A is rebound, class B(A) can no longer prove its base is hookless.
    source = "class A: ...\nimport pkg as A\nclass B(A): ...\n"
    assert module_inertness(parse(source)).reason == "nonlocal-base"


def test_local_base_with_set_name_hook_rejects_as_base() -> None:
    source = "class A:\n    def __set_name__(self, o, n): ...\nclass B(A): ...\n"
    assert module_inertness(parse(source)).reason == "nonlocal-base"


def test_nested_class_never_registers_as_a_base() -> None:
    source = "class Outer:\n    class Inner: ...\nclass C(Inner): ...\n"
    assert module_inertness(parse(source)).reason == "nonlocal-base"


def test_pep695_type_params_reject() -> None:
    assert module_inertness(parse("def f[T](x: T) -> T: ...\n")).reason == "type-params"


def test_type_alias_statement_rejects() -> None:
    assert module_inertness(parse("type Alias = int\n")).reason == "typealias"


def test_try_with_computed_except_type_rejects() -> None:
    source = "try:\n    import a\nexcept errors.Missing:\n    pass\n"
    assert module_inertness(parse(source)).reason == "computed-except-type"


def test_class_annotation_without_future_rejects() -> None:
    source = "import t\nclass C:\n    x: t.Thing = 1\n"
    assert module_inertness(parse(source)).reason == "evaluated-annotation"


def test_assert_rejects_because_its_expression_evaluates() -> None:
    assert module_inertness(parse("assert True\n")).reason == "assert"


# ---------------------------------------------------------------------------
# Pure re-exporter scanning and the star tier
# ---------------------------------------------------------------------------


def test_scan_collects_from_import_bindings() -> None:
    scan = scan_reexports(parse("from .table import Table as T\n"))
    assert scan.reason is None
    assert scan.bindings == (
        ReexportBinding(
            name="T", form=BindingForm.FROM_IMPORT, module="table", level=1, member="Table"
        ),
    )


def test_scan_expands_bare_module_import_to_every_prefix() -> None:
    scan = scan_reexports(parse("import a.b\n"))
    assert scan.bindings == (
        ReexportBinding(name="a", form=BindingForm.MODULE_IMPORT, module="a", level=0, member=""),
        ReexportBinding(name="a", form=BindingForm.MODULE_IMPORT, module="a.b", level=0, member=""),
    )


def test_scan_aliased_module_import_binds_the_leaf_only() -> None:
    scan = scan_reexports(parse("import a.b as c\n"))
    assert scan.bindings == (
        ReexportBinding(name="c", form=BindingForm.MODULE_IMPORT, module="a.b", level=0, member=""),
    )


def test_scan_records_local_literal_names() -> None:
    scan = scan_reexports(parse("__version__ = '1.0'\n__all__ = ['x']\nDEBUG: bool = False\n"))
    assert scan.local_names == ("__version__", "__all__", "DEBUG")


def test_scan_future_import_binds_locally() -> None:
    scan = scan_reexports(parse("from __future__ import annotations\n"))
    assert scan.local_names == ("annotations",)
    assert scan.bindings == ()


def test_scan_excludes_type_checking_imports_from_bindings() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from .heavy import Heavy\nfrom .light import Light\n"
    )
    scan = scan_reexports(parse(source))
    assert [binding.name for binding in scan.bindings] == ["TYPE_CHECKING", "Light"]


def test_init_type_checking_guard_with_else_rejects() -> None:
    source = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n    pass\n"
    assert pure_reexporter(parse(source)).reason == "conditional"


def test_init_annotated_metadata_needs_an_inert_annotation() -> None:
    # Without future annotations, module-level annotations evaluate eagerly.
    assert pure_reexporter(parse("V: make_type() = '1'\n")).reason == "evaluated-annotation"
    assert pure_reexporter(parse("V: str = '1'\n")).tier is ReexportTier.STRICT


def test_init_non_string_bare_expression_rejects() -> None:
    assert pure_reexporter(parse("from .a import x\n42\n")).reason == "expression"


def test_init_computed_all_rejects() -> None:
    assert pure_reexporter(parse("__all__ = sorted(['a'])\n")).reason == "non-literal-__all__"


def test_star_tier_admits_a_star_over_a_literal_all_source() -> None:
    verdict = pure_reexporter(parse("from ._impl import *\n"), lambda module, level: ("A", "B"))
    assert verdict.tier is ReexportTier.STAR_ALL


def test_star_tier_rejects_when_the_source_has_no_literal_all() -> None:
    verdict = pure_reexporter(parse("from ._impl import *\n"), lambda module, level: None)
    assert verdict.tier is None
    assert verdict.reason == "star-source-not-literal-all"


def test_single_literal_all_is_scanned_for_every_module() -> None:
    scan = scan_reexports(parse("def f(): ...\n__all__ = ['f']\n"))
    assert scan.reason == "def"
    assert scan.all_names == ("f",)


def test_augmented_all_disables_the_star_source() -> None:
    scan = scan_reexports(parse("__all__ = ['a']\n__all__ += ['b']\n"))
    assert scan.all_names is None


def test_two_literal_all_assignments_disable_the_star_source() -> None:
    scan = scan_reexports(parse("__all__ = ['a']\n__all__ = ['b']\n"))
    assert scan.all_names is None


# ---------------------------------------------------------------------------
# Bound-name sets, for the relational condition
# ---------------------------------------------------------------------------


def test_bound_names_cover_imports_assigns_defs_and_classes() -> None:
    source = (
        "import os\nimport a.b\nfrom x import y as z\n"
        "N = 1\nA, B = 1, 2\nM: int = 0\nHint: int\n"
        "def f(): ...\nclass C: ...\n"
    )
    assert bound_name_set(parse(source)) == frozenset(
        {"os", "a", "z", "N", "A", "B", "M", "f", "C"}
    )


def test_bound_names_skip_type_checking_bodies() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from pkg import Hidden\nfrom pkg import Seen\n"
    )
    assert bound_name_set(parse(source)) == frozenset({"TYPE_CHECKING", "Seen"})


def test_bound_names_union_both_branches_of_a_try() -> None:
    source = "try:\n    import ujson\nexcept ImportError:\n    import json as ujson\n"
    assert bound_name_set(parse(source)) == frozenset({"ujson"})


def test_bound_names_catch_the_symbol_rename_counterexample() -> None:
    # The design doc's counterexample 1: both revisions are whitelist-inert,
    # only the pair comparison can reject the rename.
    base = parse("def helper(): ...\n")
    head = parse("def run_helper(): ...\n")
    assert module_inertness(base).ok and module_inertness(head).ok
    assert bound_name_set(base) != bound_name_set(head)
