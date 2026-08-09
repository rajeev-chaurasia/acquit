"""The ADR 0009 adversarial gallery plus unit coverage for the folding resolver.

Every whitelisted idiom folds to the expected names, every counterexample
declines with its reason. The gallery is the prototype's 37 cases promoted
to committed tests, with one deliberate difference: percent formatting and
str.format folded in the throwaway prototype but are out of the shipped v1
grammar, so their verdicts here are declines.
"""

import ast

import pytest

from acquit.graph.resolvers.folding import (
    FOLDING_RESOLVER,
    AnchoredName,
    DynamicImportScan,
    FoldedImport,
)


def scan(source: str) -> DynamicImportScan:
    tree = ast.parse(source)
    candidate = FOLDING_RESOLVER.recognize(tree)
    assert candidate is not None, "no dynamic-import-shaped node detected"
    return FOLDING_RESOLVER.prove(candidate)


def only_fold(source: str) -> FoldedImport:
    result = scan(source)
    assert result.declined == (), result.declined
    assert len(result.folded) == 1, result.folded
    return result.folded[0]


def decline_reason(source: str) -> str:
    result = scan(source)
    assert result.folded == (), result.folded
    assert len(result.declined) == 1, result.declined
    return result.declined[0].reason


# --- the gallery: (source, verdict) -----------------------------------------
# verdict is ("fold", names, anchored) or ("decline", reason).

GALLERY = {
    "fstring_over_loop_tuple": (
        "from importlib import import_module\n"
        "for n in ('a', 'b'):\n"
        "    import_module(f'pkg.{n}')\n",
        ("fold", ("pkg.a", "pkg.b"), ()),
    ),
    "concat_module_constant": (
        "import importlib\n"
        "BASE = 'pkg.plugins'\n"
        "def load(x):\n"
        "    return importlib.import_module(BASE + '.core')\n",
        ("fold", ("pkg.plugins.core",), ()),
    ),
    "registry_values_subscript": (
        "from importlib import import_module\n"
        "_REG = {'csv': 'pkg.csv_impl', 'json': 'pkg.json_impl'}\n"
        "def __getattr__(name):\n"
        "    return import_module(_REG[name])\n",
        ("fold", ("pkg.csv_impl", "pkg.json_impl"), ()),
    ),
    "registry_keys_iteration": (
        "from importlib import import_module\n"
        "_REG = {'a': object(), 'b': object()}\n"
        "def load_all():\n"
        "    for key in _REG:\n"
        "        import_module('pkg.' + key)\n",
        ("fold", ("pkg.a", "pkg.b"), ()),
    ),
    "items_unpack_values": (
        "from importlib import import_module\n"
        "_REG = {'a': 'pkg.mod_a', 'b': 'pkg.mod_b'}\n"
        "def load_all():\n"
        "    for key, mod in _REG.items():\n"
        "        import_module(mod)\n",
        ("fold", ("pkg.mod_a", "pkg.mod_b"), ()),
    ),
    "platform_dict_subscript": (
        "import sys\n"
        "from importlib import import_module\n"
        "def pick():\n"
        "    return import_module({'linux': 'pkg.epoll', 'win32': 'pkg.select'}[sys.platform])\n",
        ("fold", ("pkg.epoll", "pkg.select"), ()),
    ),
    "comprehension_over_literals": (
        "from importlib import import_module\n"
        "mods = [import_module(f'pkg.{n}') for n in ('x', 'y')]\n",
        ("fold", ("pkg.x", "pkg.y"), ()),
    ),
    "generator_lazy_single_pass": (
        "from importlib import import_module\n"
        "gen = (import_module('p.' + n) for n in ['u', 'v'])\n",
        ("fold", ("p.u", "p.v"), ()),
    ),
    # The prototype folded these two; the shipped v1 grammar does not.
    "percent_formatting_out_of_v1": (
        "from importlib import import_module\n"
        "def load(kind='a'):\n"
        "    for k in ('a', 'b'):\n"
        "        import_module('pkg.%s' % k)\n",
        ("decline", "str-percent"),
    ),
    "str_format_out_of_v1": (
        "from importlib import import_module\n"
        "for k in ('m',):\n"
        "    import_module('pkg.{}.impl'.format(k))\n",
        ("decline", "str-format"),
    ),
    "conditional_literal_rebinding_folds_to_union": (
        "import os\n"
        "from importlib import import_module\n"
        "MOD = 'pkg.fast'\n"
        "if os.name == 'nt':\n"
        "    MOD = 'pkg.slow'\n"
        "import_module(MOD)\n",
        ("fold", ("pkg.fast", "pkg.slow"), ()),
    ),
    "local_shadowing_of_module_constant": (
        "from importlib import import_module\n"
        "NAME = 'pkg.safe'\n"
        "def load(source):\n"
        "    NAME = compute(source)\n"
        "    return import_module(NAME)\n",
        ("decline", "rebound"),
    ),
    "parameter_shadowing": (
        "from importlib import import_module\n"
        "NAME = 'pkg.safe'\n"
        "def load(NAME):\n"
        "    return import_module(NAME)\n",
        ("decline", "rebound"),
    ),
    "walrus_rebinding_of_the_constant": (
        "from importlib import import_module\n"
        "NAME = 'pkg.safe'\n"
        "if (NAME := compute()):\n"
        "    pass\n"
        "import_module(NAME)\n",
        ("decline", "rebound"),
    ),
    "registry_mutated_after_binding": (
        "from importlib import import_module\n"
        "_REG = {'a': 'pkg.a'}\n"
        "_REG['b'] = compute()\n"
        "def load(name):\n"
        "    return import_module(_REG[name])\n",
        ("decline", "subscript"),
    ),
    "registry_aliased_via_function_call": (
        "from importlib import import_module\n"
        "_REG = {'a': 'pkg.a'}\n"
        "register_plugins(_REG)\n"
        "def load(name):\n"
        "    return import_module(_REG[name])\n",
        ("decline", "subscript"),
    ),
    "registry_update_method": (
        "from importlib import import_module\n"
        "_REG = {'a': 'pkg.a'}\n"
        "_REG.update(extra)\n"
        "def load(name):\n"
        "    return import_module(_REG[name])\n",
        ("decline", "subscript"),
    ),
    "string_method_on_literal": (
        "from importlib import import_module\n"
        "for k in ('A',):\n"
        "    import_module(('pkg.' + k).lower())\n",
        ("decline", "string-method"),
    ),
    "loop_variable_rebound_in_body": (
        "from importlib import import_module\n"
        "for n in ('a', 'b'):\n"
        "    n = transform(n)\n"
        "    import_module('pkg.' + n)\n",
        ("decline", "rebound"),
    ),
    "shadowed_dunder_import": (
        "__import__ = my_loader\nfor n in ('a',):\n    __import__('pkg.' + n)\n",
        ("decline", "provenance-shadowed"),
    ),
    "look_alike_import_module_method": (
        "for n in ('a',):\n    loader.import_module('pkg.' + n)\n",
        ("decline", "provenance-receiver"),
    ),
    "partially_dynamic_iterable": (
        "from importlib import import_module\n"
        "for n in ('a', 'b', extra()):\n"
        "    import_module('pkg.' + n)\n",
        ("decline", "call"),
    ),
    "sys_platform_direct_in_fstring": (
        "import sys\nfrom importlib import import_module\nimport_module(f'pkg.{sys.platform}')\n",
        ("decline", "attribute"),
    ),
    "imported_constant_from_another_module": (
        "from importlib import import_module\n"
        "from pkg.constants import BASE\n"
        "import_module(BASE + '.impl')\n",
        ("decline", "cross-module-constant"),
    ),
    "global_write_makes_constant_dynamic": (
        "from importlib import import_module\n"
        "MODE = 'pkg.default'\n"
        "def set_mode(value):\n"
        "    global MODE\n"
        "    MODE = value\n"
        "def load():\n"
        "    return import_module(MODE)\n",
        ("decline", "rebound"),
    ),
    "global_write_of_another_literal_folds_to_union": (
        "from importlib import import_module\n"
        "MODE = 'pkg.default'\n"
        "def set_fast():\n"
        "    global MODE\n"
        "    MODE = 'pkg.fast'\n"
        "def load():\n"
        "    return import_module(MODE)\n",
        ("fold", ("pkg.default", "pkg.fast"), ()),
    ),
    "sys_modules_non_literal_key": (
        "import sys\nfor n in ('a',):\n    m = sys.modules['pkg.' + n]\n",
        ("fold", ("pkg.a",), ()),
    ),
    "fake_sys_modules_attribute_chain": (
        "for n in ('a',):\n    m = fake.sys.modules['pkg.' + n]\n",
        ("decline", "provenance-receiver"),
    ),
    "closure_over_loop_variable": (
        "from importlib import import_module\n"
        "for n in ('a', 'b'):\n"
        "    def cb():\n"
        "        return import_module('pkg.' + n)\n",
        ("fold", ("pkg.a", "pkg.b"), ()),
    ),
    "fstring_conversion_rejects": (
        "from importlib import import_module\nfor n in ('a',):\n    import_module(f'pkg.{n!r}')\n",
        ("decline", "fstring-spec"),
    ),
    "dunder_package_fold": (
        "from importlib import import_module\nimport_module(f'{__package__}.plugins')\n",
        ("fold", (), (AnchoredName(anchor="__package__", ascend=0, suffix=".plugins"),)),
    ),
    "relative_name_with_dunder_package_anchor": (
        "from importlib import import_module\nimport_module('.helpers', __package__)\n",
        ("fold", (), (AnchoredName(anchor="__package__", ascend=0, suffix=".helpers"),)),
    ),
    "aug_assigned_name": (
        "from importlib import import_module\nBASE = 'pkg'\nBASE += suffix\nimport_module(BASE)\n",
        ("decline", "rebound"),
    ),
    "django_templatetags_accumulator": (
        "from importlib import import_module\n"
        "candidates = ['pkg.templatetags']\n"
        "candidates.extend(compute_more())\n"
        "for candidate in candidates:\n"
        "    import_module(candidate)\n",
        ("decline", "iterable-name"),
    ),
    "sqlalchemy_empty_accumulator": (
        "import sys\n"
        "to_restore = []\n"
        "to_restore.append(('mod', sys.modules.pop('mod', None)))\n"
        "for name, mod in to_restore:\n"
        "    sys.modules[name] = mod\n",
        ("decline", "iterable-name"),
    ),
    "named_tuple_constant_stays_foldable": (
        "from importlib import import_module\n"
        "CANDIDATES = ('pkg.a', 'pkg.b')\n"
        "for candidate in CANDIDATES:\n"
        "    import_module(candidate)\n",
        ("fold", ("pkg.a", "pkg.b"), ()),
    ),
    "del_then_reuse_declines": (
        "from importlib import import_module\nNAME = 'pkg.a'\ndel NAME\nimport_module(NAME)\n",
        ("decline", "rebound"),
    ),
}


@pytest.mark.parametrize(("label"), sorted(GALLERY))
def test_gallery(label: str) -> None:
    source, verdict = GALLERY[label]
    if verdict[0] == "fold":
        fold = only_fold(source)
        assert fold.names == verdict[1]
        assert fold.anchored == verdict[2]
    else:
        assert decline_reason(source) == verdict[1]


def test_gallery_has_the_full_prototype_case_count() -> None:
    assert len(GALLERY) == 37


# --- named regression guards for the unsound folds the prototype produced ---


def test_django_extend_accumulator_regression_guard() -> None:
    """django's templatetags discovery: a literal list display later extended.

    An early prototype admitted all-literal list displays into the constant
    environment and folded this loop to a strict subset of what runtime
    imports. Only immutable displays may qualify by name.
    """
    source, verdict = GALLERY["django_templatetags_accumulator"]
    assert verdict[0] == "decline"
    assert decline_reason(source) == "iterable-name"


def test_sqlalchemy_append_accumulator_regression_guard() -> None:
    """sqlalchemy's test utilities: an empty accumulator drained in finally.

    The vacuous "all elements are literals" check folded this loop to the
    empty set and deleted a real taint outright. It must stay declined.
    """
    source, verdict = GALLERY["sqlalchemy_empty_accumulator"]
    assert verdict[0] == "decline"
    result = scan(source)
    # The literal pop key still resolves through the literal machinery.
    assert result.literal_names == ("mod",)
    assert result.folded == ()
    assert len(result.declined) == 1


# --- hardening beyond the prototype ------------------------------------------


def test_class_body_global_write_poisons_the_constant() -> None:
    # Class bodies execute at import time and may declare global; a binding
    # census that skips them folds a constant that is not one.
    source = (
        "from importlib import import_module\n"
        "NAME = 'pkg.a'\n"
        "class Configurator:\n"
        "    global NAME\n"
        "    NAME = compute()\n"
        "import_module(NAME)\n"
    )
    assert decline_reason(source) == "rebound"


def test_multi_target_assignment_disqualifies_the_registry() -> None:
    # _REG and ALIAS are the same object; mutations through ALIAS are
    # invisible to a mention scan keyed on _REG.
    source = (
        "from importlib import import_module\n"
        "_REG = ALIAS = {'a': 'pkg.a'}\n"
        "ALIAS['b'] = compute()\n"
        "def load(name):\n"
        "    return import_module(_REG[name])\n"
    )
    assert decline_reason(source) == "subscript"


def test_shadowed_builtin_read_disqualifies_the_registry() -> None:
    # sorted is rebound somewhere in the module, so sorted(_REG) may alias.
    source = (
        "from importlib import import_module\n"
        "_REG = {'a': 'pkg.a'}\n"
        "def evil():\n"
        "    sorted = trap\n"
        "    return sorted(_REG)\n"
        "def load(name):\n"
        "    return import_module(_REG[name])\n"
    )
    assert decline_reason(source) == "subscript"


@pytest.mark.parametrize(
    "source",
    [
        # Unpacking slices strings apart: "xy" unpacks to "x" and "y".
        "from importlib import import_module\nfor a, b in ('xy',):\n    import_module(a)\n",
        "from importlib import import_module\nfor a, b in {'xy': 1}:\n    import_module(a)\n",
        "from importlib import import_module\n"
        "CANDS = ('xy', 'zw')\n"
        "for a, b in CANDS:\n"
        "    import_module(a)\n",
    ],
    ids=["inline-strings", "dict-keys", "named-tuple"],
)
def test_unpacking_non_tuple_displays_declines(source: str) -> None:
    assert decline_reason(source) == "unpack-shape"


def test_unpacking_same_length_literal_tuples_folds_per_position() -> None:
    source = (
        "from importlib import import_module\n"
        "for name, flag in (('pkg.a', 'x'), ('pkg.b', 'y')):\n"
        "    import_module(name)\n"
    )
    assert only_fold(source).names == ("pkg.a", "pkg.b")


def test_items_keys_fold_even_when_values_do_not() -> None:
    # Key-side and value-side enumerability are independent.
    source = (
        "from importlib import import_module\n"
        "_REG = {'a': object(), 'b': object()}\n"
        "for key, value in _REG.items():\n"
        "    import_module('pkg.' + key)\n"
    )
    assert only_fold(source).names == ("pkg.a", "pkg.b")


def test_registry_get_with_default_adds_the_default() -> None:
    source = (
        "from importlib import import_module\n"
        "_REG = {'a': 'pkg.a'}\n"
        "def load(name):\n"
        "    return import_module(_REG.get(name, 'pkg.fallback'))\n"
    )
    assert only_fold(source).names == ("pkg.a", "pkg.fallback")


def test_iterator_wrapper_in_loop_header_is_out_of_v1() -> None:
    source = (
        "from importlib import import_module\n"
        "for n in sorted(('pkg.a', 'pkg.b')):\n"
        "    import_module(n)\n"
    )
    assert decline_reason(source) == "iterable-call"


def test_str_join_is_out_of_v1() -> None:
    source = "from importlib import import_module\nimport_module('.'.join(('pkg', 'mod')))\n"
    assert decline_reason(source) == "str-join"


def test_empty_inline_display_folds_to_the_empty_set() -> None:
    # The loop body never runs, so no edges is the exact bound.
    source = "from importlib import import_module\nfor n in ():\n    import_module(n)\n"
    fold = only_fold(source)
    assert fold.names == ()
    assert fold.anchored == ()


def test_keyword_name_argument_folds() -> None:
    source = "from importlib import import_module\nimport_module(name='pkg.a')\n"
    assert only_fold(source).names == ("pkg.a",)


def test_dunder_import_fromlist_adds_submodules() -> None:
    source = "for n in ('pkg.a',):\n    __import__(n, fromlist=['sub', 'other'])\n"
    assert only_fold(source).names == ("pkg.a", "pkg.a.other", "pkg.a.sub")


def test_dunder_import_star_fromlist_declines() -> None:
    # fromlist=["*"] imports whatever __all__ names; nothing bounds it.
    source = "for n in ('pkg.a',):\n    __import__(n, fromlist=['*'])\n"
    assert decline_reason(source) == "fromlist-entry"


def test_dunder_import_nonzero_level_declines() -> None:
    source = "for n in ('sub',):\n    __import__(n, globals(), locals(), [], 1)\n"
    assert decline_reason(source) == "dunder-import-level"


def test_rebound_dunder_name_declines() -> None:
    # The import machinery pre-binds __name__; a literal-rebinding union
    # would still miss the machinery's own value.
    source = "import sys\n__name__ = 'pkg.fake'\nmod = sys.modules[__name__]\n"
    assert decline_reason(source) == "dunder-rebound"


def test_relative_name_without_package_declines() -> None:
    source = "from importlib import import_module\nimport_module('.helpers')\n"
    assert decline_reason(source) == "relative-no-package"


def test_two_dot_relative_name_records_the_ascent() -> None:
    source = "from importlib import import_module\nimport_module('..sibling', __package__)\n"
    fold = only_fold(source)
    assert fold.names == ()
    assert fold.anchored == (AnchoredName(anchor="__package__", ascend=1, suffix=".sibling"),)


def test_sys_modules_method_key_folds() -> None:
    source = "import sys\nfor n in ('pkg.a',):\n    sys.modules.pop(n, None)\n"
    assert only_fold(source).names == ("pkg.a",)


def test_sys_modules_store_key_folds() -> None:
    source = "import sys\nfor n in ('pkg.a',):\n    sys.modules[n] = object()\n"
    assert only_fold(source).names == ("pkg.a",)


def test_import_sys_in_function_only_declines_module_level_site() -> None:
    # The site's scope chain has no sys binding, so provenance fails.
    source = "def late():\n    import sys\nm = sys.modules[key]\n"
    assert decline_reason(source) == "provenance-sys"


def test_fold_explosion_declines() -> None:
    parts = " + ".join(f"A{i}" for i in range(3))
    bindings = "\n".join(
        f"A{i} = 'x'\nif cond:\n    A{i} = 'y{i}{j}'" for i in range(3) for j in range(4)
    )
    # Each name folds to 5 values; the concatenation crosses 5**3 = 125,
    # then one more name pushes past the 128 cap.
    source = "from importlib import import_module\n" + bindings + f"\nimport_module({parts} + A0)\n"
    assert decline_reason(source) == "explosion"


def test_mixed_module_reports_each_site_separately() -> None:
    source = (
        "from importlib import import_module\n"
        "import_module('lit.mod')\n"
        "MOD = 'pkg.a'\n"
        "import_module(MOD)\n"
        "def load(name):\n"
        "    return import_module(name)\n"
    )
    result = scan(source)
    assert result.literal_names == ("lit.mod",)
    assert [fold.names for fold in result.folded] == [("pkg.a",)]
    assert [declined.lineno for declined in result.declined] == [6]


def test_prove_is_deterministic() -> None:
    source, _ = GALLERY["registry_values_subscript"]
    assert scan(source) == scan(source)
