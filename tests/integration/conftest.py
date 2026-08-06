"""Integration-suite configuration: the oracle marker and the recorder plugin.

The recorder is a self-contained pytest plugin written next to (never inside)
each fixture repo copy. It attributes first-party imports to the test file
that triggered them: a sys.modules snapshot is taken when pytest starts
collecting or running a test module, the delta is recorded when it finishes,
and the newly imported first-party modules are purged so the next file has to
import them again. The session-wide union of first-party files is kept too.
"""

import pytest

RECORDER_MODULE = "acquit_oracle_recorder"

RECORDER_SOURCE = '''\
"""Recording plugin: repo-relative first-party imports per collected test file."""

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(os.environ["ACQUIT_ORACLE_REPO"]).resolve()
_OUT = Path(os.environ["ACQUIT_ORACLE_OUT"])

_per_file: dict[str, set[str]] = {}
_union: set[str] = set()
_collecting: dict[str, tuple[str, set[str]]] = {}
_run_file: str | None = None
_run_before: set[str] = set()


def _relative(filename: object) -> str | None:
    if not isinstance(filename, (str, os.PathLike)):
        return None
    try:
        return Path(filename).resolve().relative_to(_REPO).as_posix()
    except ValueError:
        return None


def _first_party(names: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in names:
        module = sys.modules.get(name)
        relative = _relative(getattr(module, "__file__", None))
        if relative is not None:
            found[name] = relative
    return found


def _flush(file_key: str, before: set[str]) -> None:
    found = _first_party(set(sys.modules) - before)
    _per_file.setdefault(file_key, set()).update(found.values())
    _union.update(found.values())
    for name in found:
        del sys.modules[name]


def pytest_collectstart(collector):
    if isinstance(collector, pytest.Module):
        key = _relative(collector.path)
        if key is not None:
            _per_file.setdefault(key, set())
            _collecting[collector.nodeid] = (key, set(sys.modules))


def pytest_collectreport(report):
    pending = _collecting.pop(report.nodeid, None)
    if pending is not None:
        _flush(pending[0], pending[1])


def pytest_runtest_protocol(item, nextitem):
    global _run_file, _run_before
    key = _relative(item.path)
    if key != _run_file:
        if _run_file is not None:
            _flush(_run_file, _run_before)
        _run_file = key
        _run_before = set(sys.modules)
    return None


def pytest_sessionfinish(session, exitstatus):
    global _run_file
    if _run_file is not None:
        _flush(_run_file, _run_before)
        _run_file = None
    _union.update(_first_party(set(sys.modules)).values())
    payload = {
        "per_file": {key: sorted(paths) for key, paths in sorted(_per_file.items())},
        "union": sorted(_union),
    }
    _OUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
'''


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "oracle: runtime soundness oracle tests that run real pytest in a subprocess"
    )
