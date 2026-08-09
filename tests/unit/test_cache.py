"""Unit tests for the on-disk parse cache."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from acquit.constants import ENV_CACHE_DIR
from acquit.errors import GraphError
from acquit.graph.cache import (
    CACHE_FORMAT_VERSION,
    ParseCache,
    facts_from_dict,
    facts_to_dict,
    parse_cache_dir,
)
from acquit.graph.parse import ModuleFacts, SuspectKind, parse_module_facts

SHA = "0123abcd" * 5

RICH_SOURCE = b"""\
import os
import sys.path_hook
from ..pkg import mod as m, other
from . import sibling
from x.y import *
import importlib
__import__('lit.mod')
importlib.import_module(dynamic)
eval('1 + 1')
pytest_plugins = ['pl.one', 'pl.two']

def __getattr__(name):
    return None
"""


def rich_facts(path: str = "pkg/sub/mod.py") -> ModuleFacts:
    return parse_module_facts(RICH_SOURCE, path)


def test_round_trip(tmp_path: Path) -> None:
    cache = ParseCache(tmp_path / "parse")
    facts = rich_facts()
    cache.put(SHA, facts)
    assert cache.get(SHA) == facts


def test_round_trip_preserves_both_sys_path_suspect_kinds(tmp_path: Path) -> None:
    source = b"import sys\nsys.path.insert(0, 'v')\n\ndef late():\n    sys.path.append('r')\n"
    facts = parse_module_facts(source, "pkg/paths.py")
    assert [s.kind for s in facts.suspects] == [
        SuspectKind.SYS_PATH_MUTATION_IMPORT_TIME,
        SuspectKind.SYS_PATH_MUTATION,
    ]
    cache = ParseCache(tmp_path / "parse")
    cache.put(SHA, facts)
    assert cache.get(SHA) == facts


def test_miss_on_unknown_sha(tmp_path: Path) -> None:
    assert ParseCache(tmp_path / "parse").get(SHA) is None


def test_corrupt_json_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "parse"
    cache = ParseCache(root)
    cache.put(SHA, rich_facts())
    (root / f"{SHA}.json").write_text("{ not json", encoding="utf-8")
    assert cache.get(SHA) is None


def test_non_object_payload_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "parse"
    root.mkdir(parents=True)
    (root / f"{SHA}.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert ParseCache(root).get(SHA) is None


def test_version_bump_invalidates(tmp_path: Path) -> None:
    root = tmp_path / "parse"
    root.mkdir(parents=True)
    entry = {"version": CACHE_FORMAT_VERSION + 1, "facts": facts_to_dict(rich_facts())}
    (root / f"{SHA}.json").write_text(json.dumps(entry), encoding="utf-8")
    assert ParseCache(root).get(SHA) is None


def test_wrong_shape_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "parse"
    root.mkdir(parents=True)
    entry = {"version": CACHE_FORMAT_VERSION, "facts": {"path": "x.py"}}
    (root / f"{SHA}.json").write_text(json.dumps(entry), encoding="utf-8")
    assert ParseCache(root).get(SHA) is None


def test_put_over_existing_replaces_entry(tmp_path: Path) -> None:
    cache = ParseCache(tmp_path / "parse")
    cache.put(SHA, rich_facts("old/path.py"))
    replacement = rich_facts("new/path.py")
    cache.put(SHA, replacement)
    assert cache.get(SHA) == replacement


def test_put_leaves_only_the_entry_file(tmp_path: Path) -> None:
    root = tmp_path / "parse"
    cache = ParseCache(root)
    cache.put(SHA, rich_facts())
    cache.put("f" * 40, rich_facts())
    names = sorted(item.name for item in root.iterdir())
    assert names == sorted([f"{SHA}.json", "f" * 40 + ".json"])
    assert not any(name.endswith(".tmp") for name in names)


def test_put_failure_is_silent(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupied", encoding="utf-8")
    ParseCache(blocker).put(SHA, rich_facts())
    assert blocker.read_text(encoding="utf-8") == "occupied"


def test_get_with_unreadable_root_returns_none(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupied", encoding="utf-8")
    assert ParseCache(blocker).get(SHA) is None


def test_parse_cache_dir_honors_the_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_CACHE_DIR, str(tmp_path / "override"))
    location = parse_cache_dir(tmp_path / "repo")
    assert location.parts[: len((tmp_path / "override").parts)] == (tmp_path / "override").parts
    assert location.name == "parse"


def test_parse_cache_dir_lives_outside_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_CACHE_DIR, raising=False)
    repo = tmp_path / "repo"
    location = parse_cache_dir(repo)
    assert not location.is_relative_to(repo)


def test_parse_cache_dir_is_namespaced_per_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_CACHE_DIR, str(tmp_path / "cache"))
    first = parse_cache_dir(tmp_path / "one")
    second = parse_cache_dir(tmp_path / "two")
    assert first != second


def test_facts_dict_round_trip() -> None:
    facts = rich_facts()
    assert facts_from_dict(facts_to_dict(facts)) == facts


def test_reexport_scan_round_trips_through_json() -> None:
    # A pure-init shape populates every scan field: bindings, a star, local
    # names, and the literal __all__.
    source = b"from .a import x as y\nimport b.c\nfrom ._api import *\n__all__ = ['y']\n"
    facts = parse_module_facts(source, "pkg/__init__.py")
    assert facts.reexport.reason is None
    assert facts.reexport.all_names == ("y",)
    reloaded = json.loads(json.dumps(facts_to_dict(facts)))
    assert facts_from_dict(reloaded) == facts


def test_reexport_binding_with_an_unknown_form_is_rejected() -> None:
    facts = parse_module_facts(b"from .a import x\n", "pkg/__init__.py")
    data = facts_to_dict(facts)
    data["reexport"]["bindings"][0]["form"] = "quantum-binding"
    with pytest.raises(GraphError):
        facts_from_dict(data)


def test_facts_survive_json_serialization() -> None:
    facts = rich_facts()
    reloaded = json.loads(json.dumps(facts_to_dict(facts)))
    assert facts_from_dict(reloaded) == facts


def _drop_path(data: dict[str, Any]) -> None:
    del data["path"]


def _int_path(data: dict[str, Any]) -> None:
    data["path"] = 7


def _imports_not_a_list(data: dict[str, Any]) -> None:
    data["imports"] = "nope"


def _import_missing_level(data: dict[str, Any]) -> None:
    del data["imports"][0]["level"]


def _import_level_as_str(data: dict[str, Any]) -> None:
    data["imports"][0]["level"] = "0"


def _import_star_as_int(data: dict[str, Any]) -> None:
    data["imports"][0]["is_star"] = 1


def _unknown_suspect_kind(data: dict[str, Any]) -> None:
    data["suspects"][0]["kind"] = "quantum-import"


def _lineno_as_bool(data: dict[str, Any]) -> None:
    data["suspects"][0]["lineno"] = True


def _getattr_flag_as_str(data: dict[str, Any]) -> None:
    data["defines_module_getattr"] = "yes"


def _dyn_imports_with_int(data: dict[str, Any]) -> None:
    data["dyn_literal_imports"] = ["ok", 3]


def _drop_reexport(data: dict[str, Any]) -> None:
    del data["reexport"]


def _reexport_not_an_object(data: dict[str, Any]) -> None:
    data["reexport"] = "pure"


def _reexport_local_names_with_int(data: dict[str, Any]) -> None:
    data["reexport"]["local_names"] = ["ok", 3]


@pytest.mark.parametrize(
    "mutate",
    [
        _drop_path,
        _int_path,
        _imports_not_a_list,
        _import_missing_level,
        _import_level_as_str,
        _import_star_as_int,
        _unknown_suspect_kind,
        _lineno_as_bool,
        _getattr_flag_as_str,
        _dyn_imports_with_int,
        _drop_reexport,
        _reexport_not_an_object,
        _reexport_local_names_with_int,
    ],
)
def test_facts_from_dict_rejects_bad_shapes(mutate: Callable[[dict[str, Any]], None]) -> None:
    data = facts_to_dict(rich_facts())
    mutate(data)
    with pytest.raises(GraphError):
        facts_from_dict(data)
