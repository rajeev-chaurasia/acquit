"""Pytest plugin that applies an acquit selection file.

The plugin is inert unless ACQUIT_SELECTION_FILE is set. Before any skip is
applied the document is verified once per session: it must be a selection-v2
document whose tree fingerprint matches a fresh recompute of the working tree,
resolved against the enclosing git repository. Any failure, from a missing
file to a stale tree, means every test runs. Status is reported through
pytest_report_header (and the terminal reporter under -q), never as a warning,
so filterwarnings=error cannot turn a degraded run into a broken one.

ACQUIT_CANARY=1 switches a verified selection into canary mode: nothing is
deselected, every test runs, and at session end the outcomes of the would-be
skipped files are classified against the selection. A failure there is the
unsafe-skip signal, surfaced loudly at zero risk.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from acquit import vcs
from acquit.constants import (
    CANARY_SCHEMA,
    ENV_CANARY,
    ENV_SELECTION_FILE,
    SELECTION_SCHEMA,
    SELECTION_SIZE_CAP,
)
from acquit.report import SelectionMode, to_canonical_json

_PLUGIN_NAME = "acquit-selection"


class _Refused(Exception):
    """The selection document may not be applied; the reason is the message."""


def _parse_skip_entries(document: dict[str, Any]) -> dict[str, str]:
    entries = document.get("skip")
    if not isinstance(entries, list):
        raise _Refused("skip entries are not a list")
    parsed: dict[str, str] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("witness"), str)
        ):
            raise _Refused("malformed skip entry")
        parsed[entry["path"]] = entry["witness"]
    return parsed


def _fingerprint_exclusions(
    document: dict[str, Any], selection_path: Path, root: Path
) -> frozenset[str]:
    """Repo-relative paths exempt from the tree fingerprint: the outputs the
    selection document records, plus the selection file itself. Sound, because
    excluding acquit's own freshly written documents cannot hide a user change,
    and if a user commits these files they become tracked diff content covered
    by R001 like any other resource."""
    paths: set[str] = set()
    artifacts = document.get("artifacts")
    if isinstance(artifacts, dict):
        paths.update(value for value in artifacts.values() if isinstance(value, str))
    try:
        resolved = selection_path.resolve()
    except OSError:
        return frozenset(paths)
    if resolved.is_relative_to(root):
        paths.add(resolved.relative_to(root).as_posix())
    return frozenset(paths)


def _load_document(path: Path) -> dict[str, Any]:
    if path.stat().st_size > SELECTION_SIZE_CAP:
        raise _Refused("selection file is too large")
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise _Refused("selection document is not an object")
    if document.get("schema") != SELECTION_SCHEMA:
        raise _Refused(f"schema is not {SELECTION_SCHEMA}")
    return document


class AcquitSelectionPlugin:
    """Session-scoped state shared by the header, collection, and exit hooks."""

    def __init__(self) -> None:
        self._evaluated = False
        self._status: str | None = None
        self._skip: frozenset[str] = frozenset()
        self._witness: dict[str, str] = {}
        self._root: Path | None = None
        self._rootpath: Path | None = None
        self._emptied_run = False
        self._canary = False
        self._canary_run_all = False
        self._selection_path: Path | None = None
        self._failed: set[str] = set()

    def _refuse(self, path: str, reason: str) -> None:
        self._status = f"acquit: selection {path!r} refused ({reason}), running every test"

    def _verify(self, path: Path, config: pytest.Config) -> None:
        document = _load_document(path)
        if document.get("mode") != str(SelectionMode.SELECTIVE):
            self._status = f"acquit: selection {str(path)!r} is run-all, running every test"
            self._canary_run_all = self._canary
            return
        if not isinstance(document.get("graph_hash"), str):
            raise _Refused("selective document carries no graph hash")
        entries = _parse_skip_entries(document)
        tree = document.get("tree")
        if not isinstance(tree, dict) or not isinstance(tree.get("fingerprint"), str):
            raise _Refused("selective document carries no tree fingerprint")
        root = vcs.repo_root(Path(config.rootpath)).resolve()
        exclude = _fingerprint_exclusions(document, path, root)
        if vcs.working_tree_fingerprint(root, exclude) != tree["fingerprint"]:
            raise _Refused("tree fingerprint mismatch, the analyzed tree has moved on")
        self._skip = frozenset(entries)
        self._witness = entries
        self._root = root
        self._selection_path = path
        if self._canary:
            self._status = (
                f"acquit: canary: selection {str(path)!r} verified, "
                f"watching {len(entries)} would-be-skipped files"
            )
        else:
            self._status = f"acquit: selection {str(path)!r} applied, {len(entries)} skippable"

    def _ensure_evaluated(self, config: pytest.Config) -> None:
        if self._evaluated:
            return
        self._evaluated = True
        raw = os.environ.get(ENV_SELECTION_FILE)
        if not raw:
            return
        self._canary = os.environ.get(ENV_CANARY) == "1"
        self._rootpath = Path(config.rootpath)
        try:
            self._verify(Path(raw), config)
        except _Refused as refusal:
            self._refuse(raw, str(refusal))
        except Exception as error:  # anything surprising also fails closed
            self._refuse(raw, f"{type(error).__name__}: {error}")

    def _explicit_files(self, config: pytest.Config) -> frozenset[Path]:
        """Files the operator named on the command line, resolved at the root."""
        if config.args_source is not pytest.Config.ArgsSource.ARGS or self._root is None:
            return frozenset()
        named: set[Path] = set()
        for arg in config.args:
            file_part = Path(arg.split("::")[0])
            anchored = file_part if file_part.is_absolute() else self._root / file_part
            try:
                named.add(anchored.resolve())
            except OSError:
                continue
        return frozenset(named)

    def pytest_report_header(self, config: pytest.Config) -> list[str]:
        self._ensure_evaluated(config)
        return [] if self._status is None else [self._status]

    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        self._ensure_evaluated(config)
        # The header is suppressed under -q; degraded runs must still say so.
        if self._status is not None and config.get_verbosity() < 0:
            reporter = config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                reporter.write_line(self._status)
        # Canary watches; it never deselects.
        if self._canary or not self._skip or self._root is None:
            return

        explicit = self._explicit_files(config)
        kept: list[pytest.Item] = []
        deselected: list[pytest.Item] = []
        for item in items:
            try:
                resolved = Path(item.path).resolve()
                relative = (
                    resolved.relative_to(self._root).as_posix()
                    if resolved.is_relative_to(self._root)
                    else None
                )
            except (OSError, ValueError):
                relative = None
            if relative in self._skip and resolved not in explicit:
                deselected.append(item)
            else:
                kept.append(item)

        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = kept
            self._emptied_run = not kept

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # report.failed covers failures and errors in any phase.
        if not self._canary or self._root is None or not report.failed:
            return
        file_part = Path(report.nodeid.split("::")[0])
        base = self._rootpath or self._root
        anchored = file_part if file_part.is_absolute() else base / file_part
        try:
            resolved = anchored.resolve()
        except OSError:
            return
        if resolved.is_relative_to(self._root):
            self._failed.add(resolved.relative_to(self._root).as_posix())

    def _emit(self, config: pytest.Config, line: str) -> None:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(line)

    def _finish_canary(self, config: pytest.Config) -> None:
        if self._canary_run_all:
            self._emit(config, "acquit canary: selection was run-all, nothing to validate")
            return
        # A refused document means a plain full run; canary claims nothing.
        if self._root is None or self._selection_path is None:
            return
        alarmed = sorted(self._skip & self._failed)
        total = len(self._skip)
        for path in alarmed:
            self._emit(
                config,
                f"acquit canary: ALARM: {path} failed but was provably unaffected "
                f"(witness {self._witness[path]})",
            )
        if alarmed:
            self._emit(
                config,
                f"acquit canary: {len(alarmed)} alarm(s) across {total} would-be-skipped files",
            )
        else:
            self._emit(
                config,
                f"acquit canary: clean: all {total} would-be-skipped files passed "
                "(selection validated live)",
            )
        verdict: dict[str, Any] = {
            "schema": CANARY_SCHEMA,
            "alarms": [{"path": path, "witness": self._witness[path]} for path in alarmed],
            "would_skip": total,
            "clean": not alarmed,
        }
        target = self._selection_path.with_suffix(".canary.json")
        try:
            target.write_text(to_canonical_json(verdict), encoding="utf-8")
        except OSError as error:
            # The verdict file is telemetry; losing it must not touch the run.
            self._emit(config, f"acquit canary: could not write {str(target)!r} ({error})")

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        # An empty run because acquit proved every test unaffected is a success.
        if self._emptied_run and exitstatus == int(pytest.ExitCode.NO_TESTS_COLLECTED):
            session.exitstatus = int(pytest.ExitCode.OK)
        # Classification only; the exit status is never altered here.
        if self._canary:
            self._finish_canary(session.config)


def pytest_configure(config: pytest.Config) -> None:
    if not config.pluginmanager.has_plugin(_PLUGIN_NAME):
        config.pluginmanager.register(AcquitSelectionPlugin(), _PLUGIN_NAME)
