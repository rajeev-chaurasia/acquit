"""Adversarial pass on the ADR 0008 narrowed claim.

Every reproduction builds a two-revision repository with narrowing enabled,
establishes ground truth with real pytest runs, and drives the real pipeline.
NARROW-1 through NARROW-5 are open findings, kept as strict xfails: each one
constructs a narrowed skip whose test outcome genuinely differs between the
revisions while all six conditions hold. Their shared mechanism is that the
whitelist proves the changed file binds only its own names, but the values it
binds (constants, __all__ listings, statement order) flow at import time into
unchanged siblings that are themselves import-time-only for the victim, so
no semantic edge ever reaches back into the victim's semantic closure. The
passing tests guard the refusals that held and the replay tamper checks.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from acquit.cli import main
from acquit.errors import ExitCode
from acquit.pipeline import SelectResult, run_select
from adversarial.conftest import AdvRepo, buckets, run_git

_NARROWING_TOML = "narrowing = true\n"
_TEST_TABLE = "from pkg import Table\n\n\ndef test_table():\n    assert Table\n"
_TABLE_MODULE = "class Table:\n    pass\n"


def _enable_narrowing(adv_repo: AdvRepo) -> None:
    adv_repo.write({".acquit.toml": _NARROWING_TOML})


def _selected_reasons(result: SelectResult, path: str) -> tuple[str, ...]:
    entry = next(e for e in result.decision.selected if e.path == path)
    return entry.reasons


def _skip_is_narrowed(result: SelectResult, path: str) -> bool:
    entry = next(e for e in result.decision.skipped if e.path == path)
    return entry.narrowed


# ---------------------------------------------------------------------------
# Confirmed unsafe narrowed skips (open findings, strict xfail)
# ---------------------------------------------------------------------------


# NARROW-1: a constant flip in a whitelist-inert sibling is read at import
# time by an unchanged sibling that raises on the new value. The reader is
# reachable only through the pure init, so the changed file stays outside the
# victim's semantic closure in both graphs and all six conditions hold, yet
# importing the package fails at head for every consumer.
@pytest.mark.xfail(
    strict=True,
    reason="NARROW-1: import-time value coupling through an unchanged sibling",
)
def test_constant_flip_tripping_an_unchanged_siblings_import_guard(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": (
                '"""Pure re-exporter."""\n\n'
                "from .reader import READER_OK\nfrom .table import Table\n\n"
                '__all__ = ["READER_OK", "Table"]\n'
            ),
            "pkg/console.py": "LIMIT = 1\n",
            "pkg/reader.py": (
                "from .console import LIMIT\n\n"
                "if LIMIT != 1:\n"
                "    raise RuntimeError('reader rejects the new limit')\n"
                "READER_OK = True\n"
            ),
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    assert adv_repo.run_pytest().returncode == 0
    adv_repo.write({"pkg/console.py": "LIMIT = 2\n"})
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    if "test_table.py" in skipped:
        assert _skip_is_narrowed(result, "test_table.py")
    assert "test_table.py" not in skipped


# NARROW-2: the silent variant of NARROW-1. The unchanged sibling copies the
# flipped constant into a registry at import time, and the victim reads the
# registry at runtime through its own unchanged home module. Nothing raises;
# the narrowed skip simply hides a failing test.
@pytest.mark.xfail(
    strict=True,
    reason="NARROW-2: import-time registry mutation carries the changed value to the victim",
)
def test_constant_flip_relayed_through_an_import_time_registry_write(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": (
                '"""Pure re-exporter."""\n\n'
                "from .registry import get_limit\n"
                "from .reader import READER_OK\nfrom .table import Table\n\n"
                '__all__ = ["get_limit", "READER_OK", "Table"]\n'
            ),
            "pkg/registry.py": (
                "LIMITS = []\n\n\ndef get_limit():\n    return LIMITS[0] if LIMITS else 0\n"
            ),
            "pkg/console.py": "LIMIT = 1\n",
            "pkg/reader.py": (
                "from .console import LIMIT\nfrom .registry import LIMITS\n\n"
                "LIMITS.append(LIMIT)\nREADER_OK = True\n"
            ),
            "pkg/table.py": _TABLE_MODULE,
            "test_limit.py": (
                "from pkg import get_limit\n\n\ndef test_limit():\n    assert get_limit() == 1\n"
            ),
            "test_reader.py": (
                "from pkg import READER_OK\n\n\ndef test_reader():\n    assert READER_OK\n"
            ),
        }
    )
    base = adv_repo.commit("base")
    assert adv_repo.run_pytest().returncode == 0
    adv_repo.write({"pkg/console.py": "LIMIT = 2\n"})
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    selected, skipped, _ = buckets(result)
    # The reader-symbol consumer is correctly kept: its home pins the sibling.
    assert "test_reader.py" in selected
    if "test_limit.py" in skipped:
        assert _skip_is_narrowed(result, "test_limit.py")
    assert "test_limit.py" not in skipped


# NARROW-3: __all__ is just another literal assignment to the inertness
# whitelist and to the bound-name collector, but its VALUE is import-time
# behavior: an unchanged sibling star-importing the changed file raises
# AttributeError at head because the new listing names a missing attribute.
@pytest.mark.xfail(
    strict=True,
    reason="NARROW-3: __all__ content drives an unchanged star importer",
)
def test_all_listing_change_breaks_an_unchanged_star_importer(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": (
                '"""Pure re-exporter."""\n\n'
                "from .star import CHOSEN\nfrom .table import Table\n\n"
                '__all__ = ["CHOSEN", "Table"]\n'
            ),
            "pkg/console.py": '__all__ = ["helper"]\n\n\ndef helper():\n    return 1\n',
            "pkg/star.py": (
                "from .console import *  # noqa: F403\n\nCHOSEN = helper  # noqa: F405\n"
            ),
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    assert adv_repo.run_pytest().returncode == 0
    adv_repo.write(
        {"pkg/console.py": '__all__ = ["helper", "missing"]\n\n\ndef helper():\n    return 1\n'}
    )
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    if "test_table.py" in skipped:
        assert _skip_is_narrowed(result, "test_table.py")
    assert "test_table.py" not in skipped


# NARROW-4: condition 4 compares the resolved edge SET, which erases
# statement order. Reordering two imports inside the inert file reorders the
# unchanged siblings' import-time side effects, and the victim observes the
# order through its own home module at runtime.
@pytest.mark.xfail(
    strict=True,
    reason="NARROW-4: the edge-set comparison erases import statement order",
)
def test_import_reorder_flips_order_dependent_sibling_effects(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": (
                '"""Pure re-exporter."""\n\n'
                "from .registry import first\nfrom .console import MARK\n"
                'from .table import Table\n\n__all__ = ["first", "MARK", "Table"]\n'
            ),
            "pkg/registry.py": "ORDER = []\n\n\ndef first():\n    return ORDER[0]\n",
            "pkg/a.py": "from .registry import ORDER\n\nORDER.append('a')\n",
            "pkg/b.py": "from .registry import ORDER\n\nORDER.append('b')\n",
            "pkg/console.py": "from . import a\nfrom . import b\n\nMARK = 1\n",
            "pkg/table.py": _TABLE_MODULE,
            "test_first.py": (
                "from pkg import first\n\n\ndef test_first():\n    assert first() == 'a'\n"
            ),
        }
    )
    base = adv_repo.commit("base")
    assert adv_repo.run_pytest().returncode == 0
    adv_repo.write({"pkg/console.py": "from . import b\nfrom . import a\n\nMARK = 1\n"})
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    if "test_first.py" in skipped:
        assert _skip_is_narrowed(result, "test_first.py")
    assert "test_first.py" not in skipped


# NARROW-5: condition 5 only vets inits whose RE-EXPORT edges the route
# crosses. An impure init below the pure one is an ordinary module on the
# route, free to act on the changed value at import time, exactly like the
# reader in NARROW-1. The changed file itself is a plain inert submodule.
@pytest.mark.xfail(
    strict=True,
    reason="NARROW-5: an impure init below the pure one couples at import time",
)
def test_impure_subinit_below_the_pure_init_acts_on_the_changed_value(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": (
                '"""Pure re-exporter."""\n\nfrom .table import Table\nfrom .sub import Widget\n'
            ),
            "pkg/sub/__init__.py": (
                "from .widget import Widget\n\n"
                "if Widget.VERSION != 1:\n"
                "    raise RuntimeError('sub rejects the new widget')\n\n\n"
                "def make():\n    return Widget()\n"
            ),
            "pkg/sub/widget.py": "class Widget:\n    VERSION = 1\n",
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    assert adv_repo.run_pytest().returncode == 0
    adv_repo.write({"pkg/sub/widget.py": "class Widget:\n    VERSION = 2\n"})
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    if "test_table.py" in skipped:
        assert _skip_is_narrowed(result, "test_table.py")
    assert "test_table.py" not in skipped


# ---------------------------------------------------------------------------
# Attacks the conditions repelled (regression guards)
# ---------------------------------------------------------------------------


_INIT_HOME_SIBLING = (
    '"""Pure re-exporter."""\n\nfrom .home import api\nfrom .sibling import helper\n\n'
    '__all__ = ["api", "helper"]\n'
)
_TEST_API = "from pkg import api\n\n\ndef test_api():\n    assert api() == 1\n"


# A def body change in a sibling the home calls at runtime: the home's
# module-level import is a full edge, so condition 6 keeps the victim.
def test_def_body_change_called_by_the_home_is_refused(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": _INIT_HOME_SIBLING,
            "pkg/home.py": "from .sibling import helper\n\n\ndef api():\n    return helper()\n",
            "pkg/sibling.py": "def helper():\n    return 1\n",
            "test_api.py": _TEST_API,
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/sibling.py": "def helper():\n    return 2\n"})
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_api.py" not in skipped
    assert "narrowing-refused:inside-semantic-closure" in _selected_reasons(result, "test_api.py")


# The same coupling through a function-level import: lazy imports are still
# collected as edges, so the sibling stays in the semantic closure.
def test_lazy_function_level_import_still_pins_the_sibling(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": _INIT_HOME_SIBLING,
            "pkg/home.py": ("def api():\n    from .sibling import helper\n\n    return helper()\n"),
            "pkg/sibling.py": "def helper():\n    return 1\n",
            "test_api.py": _TEST_API,
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/sibling.py": "def helper():\n    return 2\n"})
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_api.py" not in skipped
    assert "narrowing-refused:inside-semantic-closure" in _selected_reasons(result, "test_api.py")


# The home reading the changed constant through the init itself: symbol-home
# attribution gives the home a full edge to the constant's home module.
def test_home_reading_the_constant_through_the_init_is_refused(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": (
                '"""Pure re-exporter."""\n\nfrom .console import LIMIT\nfrom .home import api\n\n'
                '__all__ = ["LIMIT", "api"]\n'
            ),
            "pkg/console.py": "LIMIT = 1\n",
            "pkg/home.py": (
                "from pkg import LIMIT\n\nDEFAULT = LIMIT\n\n\ndef api():\n    return DEFAULT\n"
            ),
            "test_api.py": _TEST_API,
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/console.py": "LIMIT = 2\n"})
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_api.py" not in skipped
    assert "narrowing-refused:inside-semantic-closure" in _selected_reasons(result, "test_api.py")


_SIBLING_TC_EXTRA = (
    "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from . import extra\n\nX = 1\n"
)
_SIBLING_TC_OTHER = (
    "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from . import other\n\nX = 1\n"
)


# A TYPE_CHECKING-only import retargeted to another module keeps the bound
# names equal, but condition 4 compares TYPE_CHECKING-collected edges too.
def test_type_checking_retarget_refuses_on_the_edge_set(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": '"""init."""\n\nfrom .table import Table\nfrom .sibling import X\n',
            "pkg/extra.py": "WIDGET = 1\n",
            "pkg/other.py": "GADGET = 1\n",
            "pkg/sibling.py": _SIBLING_TC_EXTRA,
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/sibling.py": _SIBLING_TC_OTHER})
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_table.py" not in skipped
    assert "narrowing-refused:edge-set-differs" in _selected_reasons(result, "test_table.py")


# Dropping a TYPE_CHECKING import whose destination is already imported at
# module level leaves the edge set equal, and the narrow is sound: guarded
# bodies never execute at runtime.
def test_type_checking_drop_with_an_already_imported_destination_narrows(
    adv_repo: AdvRepo,
) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": '"""init."""\n\nfrom .table import Table\nfrom .sibling import X\n',
            "pkg/extra.py": "WIDGET = 1\nVALUE = 2\n",
            "pkg/sibling.py": (
                "from typing import TYPE_CHECKING\n\nfrom .extra import VALUE\n\n"
                "if TYPE_CHECKING:\n    from .extra import WIDGET\n\nX = 1\n"
            ),
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write(
        {
            "pkg/sibling.py": (
                "from typing import TYPE_CHECKING\n\nfrom .extra import VALUE\n\nX = 1\n"
            )
        }
    )
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_table.py" in skipped
    assert _skip_is_narrowed(result, "test_table.py")


# A conditional expression is not in the inert value grammar, even over
# constants and previously bound names.
def test_conditional_expression_value_is_not_inert(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": '"""init."""\n\nfrom .table import Table\nfrom .sibling import X\n',
            "pkg/sibling.py": "FLAG = True\nX = 1 if FLAG else 2\n",
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/sibling.py": "FLAG = True\nX = 2 if FLAG else 1\n"})
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_table.py" not in skipped
    assert "narrowing-refused:not-import-inert" in _selected_reasons(result, "test_table.py")


# Dropping an import while keeping its bound name alive as a constant leaves
# the bound-name set equal; the edge set comparison still refuses, and the
# base-closure-only asymmetry never reaches a witness.
def test_dropped_import_with_stable_bound_names_refuses_on_the_edge_set(
    adv_repo: AdvRepo,
) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": '"""init."""\n\nfrom .table import Table\nfrom .sibling import X\n',
            "pkg/extra.py": "SIDE = 1\n",
            "pkg/sibling.py": "import pkg.extra as _px\n\nX = 1\n",
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/sibling.py": "_px = None\n\nX = 1\n"})
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_table.py" not in skipped
    assert "narrowing-refused:edge-set-differs" in _selected_reasons(result, "test_table.py")


# A changed file that is itself an init on the route never narrows, nested
# or not, even when the edit keeps it a pure re-exporter.
def test_changed_nested_init_on_the_route_never_narrows(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": '"""init."""\n\nfrom .table import Table\nfrom .sub import Widget\n',
            "pkg/sub/__init__.py": 'from .widget import Widget\n\n__version__ = "1"\n',
            "pkg/sub/widget.py": "class Widget:\n    pass\n",
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/sub/__init__.py": 'from .widget import Widget\n\n__version__ = "2"\n'})
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_table.py" not in skipped
    assert "narrowing-refused:changed-init" in _selected_reasons(result, "test_table.py")


# A star-tier init whose star source stops proving a single literal __all__
# at head loses its proof, and condition 5 refuses for lack of a relied init.
def test_star_source_losing_its_literal_all_refuses(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": (
                '"""init."""\n\nfrom .table import Table\nfrom ._api import *  # noqa: F403\n'
            ),
            "pkg/_api.py": '__all__ = ["x"]\n\n\ndef x():\n    return 1\n',
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    # A second literal assignment keeps the file inert and the bound names
    # equal while making the star-bound set unprovable.
    adv_repo.write(
        {"pkg/_api.py": '__all__ = ["x"]\n__all__ = ["x"]\n\n\ndef x():\n    return 1\n'}
    )
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_table.py" not in skipped
    assert "narrowing-refused:impure-init" in _selected_reasons(result, "test_table.py")


# A root conftest importing the package plainly takes the full fan-out, so
# every test scopes to it and nothing narrows (the design's documented cost).
def test_plain_conftest_import_of_the_package_disables_narrowing(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "conftest.py": "import pkg  # noqa: F401\n",
            "pkg/__init__.py": (
                '"""init."""\n\nfrom .table import Table\nfrom .console import Console\n'
            ),
            "pkg/console.py": "LIMIT = 1\n\n\nclass Console:\n    pass\n",
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/console.py": "LIMIT = 2\n\n\nclass Console:\n    pass\n"})
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_table.py" not in skipped
    assert "narrowing-refused:inside-semantic-closure" in _selected_reasons(result, "test_table.py")


# A git-detected rename of the import-time-only sibling: neither the origin
# nor the destination is modified in place, so condition 1 refuses.
def test_renamed_sibling_never_narrows(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": (
                '"""init."""\n\nfrom .table import Table\nfrom .reader import READY\n'
            ),
            "pkg/console.py": (
                "LIMIT = 1\n# ballast line one\n# ballast line two\n# ballast line three\n"
            ),
            "pkg/reader.py": "from .console import LIMIT\n\nREADY = LIMIT == 1\n",
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    run_git(adv_repo.path, "mv", "pkg/console.py", "pkg/console2.py")
    adv_repo.write({"pkg/reader.py": "from .console2 import LIMIT\n\nREADY = LIMIT == 1\n"})
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_table.py" not in skipped
    assert "narrowing-refused:not-modified-in-place" in _selected_reasons(result, "test_table.py")


# Narrowing engages below a namespace subpackage: there is no init to prove
# there, and the top init's proof carries the route.
def test_namespace_subpackage_sibling_narrows(adv_repo: AdvRepo) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": (
                '"""init."""\n\nfrom .table import Table\n'
                "from .ns.impl import X\nfrom .ns.impl2 import Y\n"
            ),
            "pkg/ns/impl.py": "X = 1\n",
            "pkg/ns/impl2.py": "Y = 1\n",
            "pkg/table.py": _TABLE_MODULE,
            "test_table.py": _TEST_TABLE,
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/ns/impl2.py": "Y = 2\n"})
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_table.py" in skipped
    assert _skip_is_narrowed(result, "test_table.py")


# ---------------------------------------------------------------------------
# Attacks on replay
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _dump(document: dict[str, Any], path: Path) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _select_docs(repo: Path, base: str, head: str, out: Path) -> tuple[Path, Path]:
    report = out / "report.json"
    witnesses = out / "witnesses.json"
    with pytest.MonkeyPatch.context() as patcher:
        patcher.chdir(repo)
        exit_code = main(
            [
                "select",
                "--base",
                base,
                "--head",
                head,
                "--report",
                str(report),
                "--selection",
                str(out / "selection.json"),
                "--witnesses",
                str(witnesses),
            ]
        )
    assert exit_code == ExitCode.OK
    return report, witnesses


@pytest.fixture(scope="module")
def two_file_narrowed_docs(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, Path]:
    """A safe narrowed run whose one witness excuses two intersecting files."""
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    repo = tmp_path_factory.mktemp("narrowed-two")
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.name", "Acquit Adversary")
    run_git(repo, "config", "user.email", "adversary@acquit.invalid")
    run_git(repo, "config", "commit.gpgsign", "false")
    run_git(repo, "config", "core.autocrlf", "false")
    adv = AdvRepo(repo)
    adv.write(
        {
            ".gitignore": ".acquit/\n__pycache__/\n*.pyc\n.pytest_cache/\n",
            ".acquit.toml": _NARROWING_TOML,
            "pkg/__init__.py": (
                '"""Pure re-exporter."""\n\nfrom .alpha import Alpha\nfrom .beta import Beta\n'
                'from .gamma import Gamma\n\n__all__ = ["Alpha", "Beta", "Gamma"]\n'
            ),
            "pkg/alpha.py": 'class Alpha:\n    def go(self):\n        return "alpha"\n',
            "pkg/beta.py": 'class Beta:\n    def go(self):\n        return "beta"\n',
            "pkg/gamma.py": "class Gamma:\n    pass\n",
            "test_gamma.py": "from pkg import Gamma\n\n\ndef test_gamma():\n    assert Gamma\n",
            "test_alpha.py": "from pkg import Alpha\n\n\ndef test_alpha():\n    assert Alpha\n",
        }
    )
    base = adv.commit("base")
    adv.write(
        {
            "pkg/alpha.py": 'class Alpha:\n    def go(self):\n        return "alpha2"\n',
            "pkg/beta.py": 'class Beta:\n    def go(self):\n        return "beta2"\n',
        }
    )
    head = adv.commit("head")
    out = tmp_path_factory.mktemp("narrowed-two-docs")
    report, witnesses = _select_docs(repo, base, head, out)
    document = _load(witnesses)
    entry = next(w for w in document["witnesses"] if w.get("narrowed"))
    assert [item["path"] for item in entry["narrowed"]] == ["pkg/alpha.py", "pkg/beta.py"]
    return repo, report, witnesses


def _tampered_replay(
    docs: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> int:
    repo, report, witnesses = docs
    document = _load(witnesses)
    entry = next(w for w in document["witnesses"] if w.get("narrowed"))
    mutate(entry)
    tampered = _dump(document, tmp_path / "witnesses.json")
    monkeypatch.chdir(repo)
    return main(["replay", str(report), "--witnesses", str(tampered)])


def test_replay_detects_a_tampered_head_blob(
    two_file_narrowed_docs: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _tampered_replay(
        two_file_narrowed_docs,
        tmp_path,
        monkeypatch,
        lambda entry: entry["narrowed"][0].__setitem__("head_blob", "0" * 40),
    )
    assert code == ExitCode.REPLAY_MISMATCH
    assert "blob sha mismatch for pkg/alpha.py" in capsys.readouterr().err


def test_replay_detects_a_tampered_relied_init_path(
    two_file_narrowed_docs: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _tampered_replay(
        two_file_narrowed_docs,
        tmp_path,
        monkeypatch,
        lambda entry: entry["narrowed"][0]["inits"][0].__setitem__("path", "pkg/other/__init__.py"),
    )
    assert code == ExitCode.REPLAY_MISMATCH
    assert "relied inits mismatch for pkg/alpha.py (condition 5)" in capsys.readouterr().err


def test_replay_rejects_a_tampered_changed_list(
    two_file_narrowed_docs: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _tampered_replay(
        two_file_narrowed_docs,
        tmp_path,
        monkeypatch,
        lambda entry: entry.__setitem__("changed", ["pkg/alpha.py"]),
    )
    assert code == ExitCode.REPLAY_MISMATCH
    assert "failed verification" in capsys.readouterr().err


def test_replay_rejects_a_narrowed_witness_wearing_the_disjoint_claim(
    two_file_narrowed_docs: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _tampered_replay(
        two_file_narrowed_docs,
        tmp_path,
        monkeypatch,
        lambda entry: entry.__setitem__("claim", "closure(test) does not intersect changed set"),
    )
    assert code == ExitCode.REPLAY_MISMATCH
    assert "failed verification" in capsys.readouterr().err


def test_replay_rejects_a_shortened_narrowed_listing(
    two_file_narrowed_docs: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _tampered_replay(
        two_file_narrowed_docs,
        tmp_path,
        monkeypatch,
        lambda entry: entry.__setitem__("narrowed", entry["narrowed"][:1]),
    )
    assert code == ExitCode.REPLAY_MISMATCH
    assert "failed verification" in capsys.readouterr().err


# Replay re-derives the same six conditions, so it verifies the NARROW-2
# witness rather than catching it: the blind spot is shared by construction.
# Remove together with the NARROW-2 xfail when the conditions are extended.
def test_replay_verifies_the_narrow2_unsafe_witness(
    adv_repo: AdvRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _enable_narrowing(adv_repo)
    adv_repo.write(
        {
            "pkg/__init__.py": (
                '"""Pure re-exporter."""\n\n'
                "from .registry import get_limit\n"
                "from .reader import READER_OK\nfrom .table import Table\n\n"
                '__all__ = ["get_limit", "READER_OK", "Table"]\n'
            ),
            "pkg/registry.py": (
                "LIMITS = []\n\n\ndef get_limit():\n    return LIMITS[0] if LIMITS else 0\n"
            ),
            "pkg/console.py": "LIMIT = 1\n",
            "pkg/reader.py": (
                "from .console import LIMIT\nfrom .registry import LIMITS\n\n"
                "LIMITS.append(LIMIT)\nREADER_OK = True\n"
            ),
            "pkg/table.py": _TABLE_MODULE,
            "test_limit.py": (
                "from pkg import get_limit\n\n\ndef test_limit():\n    assert get_limit() == 1\n"
            ),
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/console.py": "LIMIT = 2\n"})
    head = adv_repo.commit("head")
    report, witnesses = _select_docs(adv_repo.path, base, head, tmp_path)
    document = _load(witnesses)
    entry = next(w for w in document["witnesses"] if w.get("narrowed"))
    assert entry["test"] == "test_limit.py"
    monkeypatch.chdir(adv_repo.path)

    exit_code = main(["replay", str(report), "--witnesses", str(witnesses)])

    assert exit_code == ExitCode.OK
    assert "replay ok" in capsys.readouterr().out
