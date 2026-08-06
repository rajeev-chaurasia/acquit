"""Pytest plugin that applies an acquit selection file.

The plugin is inert unless ACQUIT_SELECTION_FILE is set. A missing, unreadable,
or malformed file means every test runs, with a loud warning.
"""

import json
import os
import warnings
from pathlib import Path
from typing import Any

import pytest

from acquit.constants import ENV_SELECTION_FILE, SELECTION_SCHEMA
from acquit.report import SelectionMode


def _load_selection(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema") != SELECTION_SCHEMA:
        return None
    skip = document.get("skip")
    if not isinstance(skip, list) or not all(isinstance(entry, str) for entry in skip):
        return None
    return document


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    selection_path = os.environ.get(ENV_SELECTION_FILE)
    if not selection_path:
        return

    selection = _load_selection(Path(selection_path))
    if selection is None:
        warnings.warn(
            pytest.PytestWarning(
                f"acquit: selection file {selection_path!r} is missing or invalid, "
                "running every test"
            ),
            stacklevel=2,
        )
        return
    if selection.get("mode") != str(SelectionMode.SELECTIVE):
        return

    skip = set(selection["skip"])
    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        relative = Path(item.path).resolve().relative_to(config.rootpath).as_posix()
        (deselected if relative in skip else kept).append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
