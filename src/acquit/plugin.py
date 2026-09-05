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
import sys
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
        self._failed_nodes: dict[str, set[str]] = {}
        self._outcomes: dict[str, str] = {}
        self._observations: dict[str, dict[str, Any]] = {}
        self._static_selected: dict[str, list[str]] = {}
        self._static_always_run: dict[str, str] = {}
        self._canary_context: dict[str, Any] = {}
        self._refusal_reason: str | None = None

    def _refuse(self, path: str, reason: str) -> None:
        self._status = f"acquit: selection {path!r} refused ({reason}), running every test"
        self._refusal_reason = reason

    def _verify(self, path: Path, config: pytest.Config) -> None:
        document = _load_document(path)
        self._selection_path = path
        self._canary_context = {
            "graph_hash": document.get("graph_hash"),
            "tree": document.get("tree"),
            "fallback": None,
            "changed": [],
        }
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
        self._root = root
        exclude = _fingerprint_exclusions(document, path, root)
        if vcs.working_tree_fingerprint(root, exclude) != tree["fingerprint"]:
            raise _Refused("tree fingerprint mismatch, the analyzed tree has moved on")
        self._skip = frozenset(entries)
        self._witness = entries
        self._root = root
        self._load_static_context(document)
        if self._canary:
            self._status = (
                f"acquit: canary: selection {str(path)!r} verified, "
                f"watching {len(entries)} would-be-skipped files"
            )
        else:
            self._status = f"acquit: selection {str(path)!r} applied, {len(entries)} skippable"

    def _load_static_context(self, document: dict[str, Any]) -> None:
        """Load optional diagnostic data."""
        raw = document.get("canary")
        if not isinstance(raw, dict):
            return
        fallback = raw.get("fallback")
        if isinstance(fallback, list):
            self._canary_context["fallback"] = [
                entry
                for entry in fallback
                if isinstance(entry, dict)
                and isinstance(entry.get("rule"), str)
                and isinstance(entry.get("reason"), str)
            ]
        changed = raw.get("changed")
        if isinstance(changed, list):
            self._canary_context["changed"] = [
                entry
                for entry in changed
                if isinstance(entry, dict)
                and isinstance(entry.get("path"), str)
                and isinstance(entry.get("kind"), str)
                and isinstance(entry.get("status"), str)
            ]
        selected = raw.get("selected")
        if isinstance(selected, list):
            for entry in selected:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    continue
                reasons = entry.get("reasons")
                self._static_selected[entry["path"]] = (
                    [value for value in reasons if isinstance(value, str)]
                    if isinstance(reasons, list)
                    else []
                )
        always_run = raw.get("always_run")
        if isinstance(always_run, list):
            for entry in always_run:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("path"), str)
                    and isinstance(entry.get("finding"), str)
                ):
                    self._static_always_run[entry["path"]] = entry["finding"]

    def _relative_path(self, path: Path) -> str | None:
        if self._root is None:
            return None
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(self._root).as_posix()
        except (OSError, ValueError):
            return None
        return str(Path(relative).with_suffix(".py")) if relative.endswith(".pyc") else relative

    def _item_path(self, item: pytest.Item) -> str | None:
        return self._relative_path(Path(item.path))

    def _node_path(self, nodeid: str) -> str | None:
        if self._root is None:
            return None
        file_part = Path(nodeid.split("::")[0])
        base = self._rootpath or self._root
        anchored = file_part if file_part.is_absolute() else base / file_part
        return self._relative_path(anchored)

    def _project_modules(self) -> set[str]:
        """Return repo-relative loaded module paths."""
        paths: set[str] = set()
        for module in tuple(sys.modules.values()):
            source = getattr(module, "__file__", None)
            if isinstance(source, str):
                relative = self._relative_path(Path(source))
                if relative is not None:
                    paths.add(relative)
        return paths

    def _fixture_observations(
        self, item: pytest.Item
    ) -> tuple[list[dict[str, str]], list[str], list[str]]:
        """Return fixture and plugin observations."""
        fixture_info = getattr(item, "_fixtureinfo", None)
        by_name = getattr(fixture_info, "name2fixturedefs", None)
        if not isinstance(by_name, dict):
            return [], [], ["fixture-chain-unavailable"]
        fixtures: list[dict[str, str]] = []
        plugins: set[str] = set()
        unknown: list[str] = []
        names = getattr(item, "fixturenames", ())
        for name in sorted(value for value in names if isinstance(value, str)):
            definitions = by_name.get(name)
            if not isinstance(definitions, (list, tuple)) or not definitions:
                unknown.append(f"fixture-provider-unavailable:{name}")
                continue
            fixture = definitions[-1]
            function = getattr(fixture, "func", None)
            code = getattr(function, "__code__", None)
            filename = getattr(code, "co_filename", None)
            relative = self._relative_path(Path(filename)) if isinstance(filename, str) else None
            if relative is not None:
                source = "conftest" if relative.endswith("conftest.py") else "project"
                fixtures.append({"name": name, "provider": relative, "source": source})
                continue
            module_name = getattr(function, "__module__", None)
            if isinstance(module_name, str) and module_name:
                plugins.add(module_name)
            else:
                unknown.append(f"fixture-provider-unavailable:{name}")
        return fixtures, sorted(plugins), unknown

    def _selection_context_for(self, path: str | None) -> dict[str, Any]:
        if path is not None and path in self._witness:
            return {
                "source": "static",
                "classification": "would-skip",
                "witness": self._witness[path],
            }
        if path is not None and path in self._static_always_run:
            return {
                "source": "static",
                "classification": "always-run",
                "finding": self._static_always_run[path],
            }
        return {
            "source": "static",
            "classification": "selected",
            "reasons": [] if path is None else self._static_selected.get(path, []),
        }

    def _record_observation(
        self,
        item: pytest.Item,
        before: set[str],
        fixtures: list[dict[str, str]],
        plugins: list[str],
        unknown: list[str],
    ) -> None:
        path = self._item_path(item)
        after = self._project_modules()
        if before:
            unknown.append("project-modules-already-loaded-before-test")
        runtime_imports = sorted(after - before)
        self._observations[item.nodeid] = {
            "nodeid": item.nodeid,
            "path": path,
            "outcome": self._outcomes.get(item.nodeid, "unknown"),
            "selection": self._selection_context_for(path),
            "dependencies": {
                "modules": [
                    {"path": module, "kind": "runtime-import"} for module in runtime_imports
                ],
                "fixtures": sorted(fixtures, key=lambda entry: (entry["name"], entry["provider"])),
                "plugins": [{"name": name} for name in plugins],
                "unknown": sorted(set(unknown)),
            },
        }

    def _ensure_evaluated(self, config: pytest.Config) -> None:
        if self._evaluated:
            return
        self._evaluated = True
        raw = os.environ.get(ENV_SELECTION_FILE)
        if not raw:
            return
        self._canary = os.environ.get(ENV_CANARY) == "1"
        self._rootpath = Path(config.rootpath)
        self._selection_path = Path(raw)
        try:
            self._root = vcs.repo_root(self._rootpath).resolve()
        except Exception:
            self._root = None
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

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> Any:
        """Observe one test protocol without altering it."""
        if not self._canary or self._root is None:
            yield
            return
        before = self._project_modules()
        fixtures, plugins, unknown = self._fixture_observations(item)
        yield
        self._record_observation(item, before, fixtures, plugins, unknown)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if not self._canary or self._root is None:
            return
        if report.failed:
            self._outcomes[report.nodeid] = "failed"
        elif report.skipped and report.nodeid not in self._outcomes:
            self._outcomes[report.nodeid] = "skipped"
        elif report.when == "call" and report.passed and report.nodeid not in self._outcomes:
            self._outcomes[report.nodeid] = "passed"
        if not report.failed:
            return
        relative = self._node_path(report.nodeid)
        if relative is not None:
            self._failed.add(relative)
            self._failed_nodes.setdefault(relative, set()).add(report.nodeid)

    def _emit(self, config: pytest.Config, line: str) -> None:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(line)

    def _finish_canary(self, config: pytest.Config) -> None:
        if self._selection_path is None:
            return
        status = "verified"
        if self._refusal_reason is not None:
            status = "refused"
        elif self._canary_run_all:
            status = "run-all"
            self._emit(config, "acquit canary: selection was run-all, nothing to validate")
        elif self._root is None:
            status = "refused"
        alarmed = sorted(self._skip & self._failed) if status == "verified" else []
        total = len(self._skip)
        for path in alarmed:
            self._emit(
                config,
                f"acquit canary: ALARM: {path} failed but was provably unaffected "
                f"(witness {self._witness[path]})",
            )
        if status == "verified":
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
        missed = [
            {
                "severity": "high",
                "path": path,
                "witness": self._witness[path],
                "nodes": sorted(self._failed_nodes.get(path, set())),
            }
            for path in alarmed
        ]
        observations = [self._observations[nodeid] for nodeid in sorted(self._observations)]
        runtime_edges = [
            {"test": entry["path"], "module": module["path"], "kind": module["kind"]}
            for entry in observations
            if isinstance(entry.get("path"), str)
            for module in entry["dependencies"]["modules"]
        ]
        outcomes = [entry["outcome"] for entry in observations]
        verdict: dict[str, Any] = {
            "schema": CANARY_SCHEMA,
            "selection": {
                "status": status,
                "mode": "selective" if status == "verified" else "run-all",
                "graph_hash": self._canary_context.get("graph_hash"),
                "tree": self._canary_context.get("tree"),
                "reason": self._refusal_reason,
                "fallback": self._canary_context.get("fallback"),
                "changed": self._canary_context.get("changed"),
            },
            "stats": {
                "collected": len(observations),
                "passed": outcomes.count("passed"),
                "failed": outcomes.count("failed"),
                "skipped": outcomes.count("skipped"),
                "would_skip": total,
            },
            "tests": observations,
            "observations": {"runtime_edges": runtime_edges},
            "shadow_validation": {
                "status": "missed-impact"
                if missed
                else ("incomplete" if status != "verified" else "clean"),
                "missed_impact": missed,
            },
        }
        summary = self._canary_summary(verdict)
        json_target = self._selection_path.with_suffix(".canary.json")
        markdown_target = self._selection_path.with_suffix(".canary.md")
        try:
            json_target.write_text(to_canonical_json(verdict), encoding="utf-8")
            markdown_target.write_text(summary, encoding="utf-8")
        except OSError as error:
            self._emit(config, f"acquit canary: could not write evidence ({error})")

    @staticmethod
    def _canary_summary(verdict: dict[str, Any]) -> str:
        """Render the Markdown summary."""
        selection = verdict["selection"]
        stats = verdict["stats"]
        shadow = verdict["shadow_validation"]
        lines = [
            "# Acquit canary evidence",
            "",
            f"- Selection: `{selection['status']}` ({selection['mode']})",
            f"- Tests observed: {stats['collected']}; would skip: {stats['would_skip']}",
            f"- Runtime import edges: {len(verdict['observations']['runtime_edges'])}",
            f"- Shadow validation: `{shadow['status']}`",
        ]
        if shadow["missed_impact"]:
            lines.append(f"- High-severity missed-impact events: {len(shadow['missed_impact'])}")
        if isinstance(selection["reason"], str):
            lines.append(f"- Evidence unavailable: {selection['reason']}")
        fallback = selection["fallback"]
        if isinstance(fallback, list) and fallback:
            lines.append(f"- Conservative fallback reasons: {len(fallback)}")
        return "\n".join(lines) + "\n"

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
