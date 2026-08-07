"""On-disk parse cache keyed by git blob sha.

Facts for a blob never change, so entries are content-addressed JSON files.
Every failure path degrades to a re-parse (rule R017): get answers None
instead of raising, and put swallows I/O errors, because a broken cache must
never break analysis.
"""

import hashlib
import json
import os
import sys
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Final

from acquit.constants import ENV_CACHE_DIR
from acquit.errors import GraphError
from acquit.graph.parse import ImportStmt, ModuleFacts, Suspect, SuspectKind

# Bump whenever parser semantics change: cached ModuleFacts for an unchanged
# blob must never outlive the rules that produced them. Version 4 split the
# sys.path suspect into import-time and runtime kinds.
CACHE_FORMAT_VERSION: Final = 4


def parse_cache_dir(repo_root: Path) -> Path:
    """Where the parse cache for one repository lives: outside the checkout.

    A cache inside the working tree could be preseeded by the very diff under
    analysis, so entries land under ACQUIT_CACHE_DIR when set, otherwise the
    platform user cache directory, namespaced per resolved repository root.
    """
    namespace = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:16]
    override = os.environ.get(ENV_CACHE_DIR)
    if override:
        base = Path(override)
    elif sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / "acquit"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = (Path(xdg) if xdg else Path.home() / ".cache") / "acquit"
    return base / namespace / "parse"


def facts_to_dict(facts: ModuleFacts) -> dict[str, Any]:
    """Convert ModuleFacts to a JSON-serializable dict."""
    return {
        "path": facts.path,
        "imports": [
            {
                "module": stmt.module,
                "names": list(stmt.names),
                "level": stmt.level,
                "is_star": stmt.is_star,
            }
            for stmt in facts.imports
        ],
        "dyn_literal_imports": list(facts.dyn_literal_imports),
        "suspects": [
            {"kind": suspect.kind.value, "lineno": suspect.lineno} for suspect in facts.suspects
        ],
        "defines_module_getattr": facts.defines_module_getattr,
        "pytest_plugins_decl": list(facts.pytest_plugins_decl),
    }


def facts_from_dict(data: dict[str, Any]) -> ModuleFacts:
    """Rebuild ModuleFacts from its JSON form. Raises GraphError on shape mismatch."""
    try:
        return ModuleFacts(
            path=_expect_str(data["path"]),
            imports=tuple(_import_from_dict(item) for item in _expect_items(data["imports"])),
            dyn_literal_imports=_expect_str_tuple(data["dyn_literal_imports"]),
            suspects=tuple(_suspect_from_dict(item) for item in _expect_items(data["suspects"])),
            defines_module_getattr=_expect_bool(data["defines_module_getattr"]),
            pytest_plugins_decl=_expect_str_tuple(data["pytest_plugins_decl"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise GraphError(f"cached facts have an unexpected shape: {error!r}") from error


class ParseCache:
    """Store of parsed ModuleFacts, one JSON file per blob sha."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _entry(self, blob_sha: str) -> Path:
        return self._root / f"{blob_sha}.json"

    def get(self, blob_sha: str) -> ModuleFacts | None:
        """Return the cached facts for a blob, or None so the caller re-parses."""
        try:
            data = json.loads(self._entry(blob_sha).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != CACHE_FORMAT_VERSION:
            return None
        payload = data.get("facts")
        if not isinstance(payload, dict):
            return None
        try:
            return facts_from_dict(payload)
        except GraphError:
            return None

    def put(self, blob_sha: str, facts: ModuleFacts) -> None:
        """Write one entry atomically; I/O failures are swallowed."""
        payload = json.dumps(
            {"version": CACHE_FORMAT_VERSION, "facts": facts_to_dict(facts)},
            sort_keys=True,
        )
        entry = self._entry(blob_sha)
        tmp = entry.with_name(f"{entry.name}.{uuid.uuid4().hex}.tmp")
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(entry)
        except OSError:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)


def _import_from_dict(item: Mapping[str, Any]) -> ImportStmt:
    return ImportStmt(
        module=_expect_opt_str(item["module"]),
        names=_expect_str_tuple(item["names"]),
        level=_expect_int(item["level"]),
        is_star=_expect_bool(item["is_star"]),
    )


def _suspect_from_dict(item: Mapping[str, Any]) -> Suspect:
    return Suspect(kind=SuspectKind(_expect_str(item["kind"])), lineno=_expect_int(item["lineno"]))


def _expect_str(value: object) -> str:
    if isinstance(value, str):
        return value
    raise GraphError(f"expected a string, got {type(value).__name__}")


def _expect_opt_str(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise GraphError(f"expected a string or null, got {type(value).__name__}")


def _expect_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise GraphError(f"expected a bool, got {type(value).__name__}")


def _expect_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise GraphError(f"expected an int, got {type(value).__name__}")


def _expect_str_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(_expect_str(item) for item in value)
    raise GraphError(f"expected a list of strings, got {type(value).__name__}")


def _expect_items(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    raise GraphError(f"expected a list of objects, got {type(value).__name__}")
