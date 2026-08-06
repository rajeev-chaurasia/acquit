"""Command line entry point.

Every failure path converges on a run-all report. The tool may only be wrong
in the safe direction.
"""

import argparse
import json
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from acquit import __version__
from acquit.constants import (
    DEFAULT_REPORT_FILE,
    DEFAULT_SELECTION_FILE,
    DEFAULT_WITNESSES_FILE,
)
from acquit.errors import AcquitError, ExitCode
from acquit.explain import explain_lines
from acquit.graph.model import NodeKind
from acquit.pipeline import SelectResult, run_select, snapshot_working_tree
from acquit.policy.model import Finding, RuleId, Scope, ScopeKind
from acquit.replay import run_replay
from acquit.report import (
    RunInfo,
    SelectionMode,
    build_report,
    build_run_all_report,
    build_selection,
    build_selection_doc,
    build_witnesses_doc,
    to_canonical_json,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acquit")
    parser.add_argument("--version", action="version", version=f"acquit {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    select = subcommands.add_parser("select", help="decide which tests must run for a diff")
    select.add_argument("--base", required=True, help="base git ref")
    select.add_argument("--head", help="head git ref, defaults to the working tree")
    select.add_argument("--report", default=DEFAULT_REPORT_FILE, help="report output path")
    select.add_argument("--selection", default=DEFAULT_SELECTION_FILE, help="selection output path")
    select.add_argument("--witnesses", default=DEFAULT_WITNESSES_FILE, help="witnesses output path")
    select.add_argument("--durations", help="json file mapping test paths to seconds")

    subcommands.add_parser("analyze", help="build the dependency graph and print its health")

    explain = subcommands.add_parser("explain", help="explain the decision for one test file")
    explain.add_argument("test", help="repo-relative test file path")
    explain.add_argument("--base", required=True, help="base git ref")
    explain.add_argument("--head", help="head git ref, defaults to the working tree")

    replay = subcommands.add_parser("replay", help="re-verify the witnesses behind a report")
    replay.add_argument("report", help="path to an acquit report file")
    replay.add_argument("--witnesses", default=DEFAULT_WITNESSES_FILE, help="witnesses file path")
    return parser


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load_durations(path_arg: str | None) -> dict[str, float] | None:
    if path_arg is None:
        return None
    data = json.loads(Path(path_arg).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AcquitError(f"{path_arg}: durations must be a json object")
    durations: dict[str, float] = {}
    for key, value in data.items():
        if (
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int | float)
        ):
            raise AcquitError(f"{path_arg}: durations must map test paths to seconds")
        durations[key] = float(value)
    return durations


def _summary(result: SelectResult) -> str:
    decision = result.decision
    total = sum(1 for node in result.head.graph.nodes.values() if node.kind is NodeKind.TEST)
    if decision.mode is SelectionMode.SELECTIVE:
        return (
            f"acquit: selective: {len(decision.selected)} selected, "
            f"{len(decision.always_run)} always-run, "
            f"{len(decision.skipped)} skipped of {total} tests"
        )
    findings = result.outcome.findings
    if findings:
        top = "; ".join(f"{finding.rule}:{finding.subject}" for finding in findings[:3])
        if len(findings) > 3:
            top += f" and {len(findings) - 3} more"
    else:
        top = "no test could be proven unaffected"
    return f"acquit: run-all: {top} ({total} tests)"


def _run_select(args: argparse.Namespace) -> int:
    created_at = _now()
    durations = _load_durations(args.durations)
    result = run_select(args.base, args.head, Path.cwd())
    graph_hash = result.head.graph.graph_hash
    report = build_report(result, created_at=created_at, durations=durations)
    selection = build_selection_doc(result.decision, graph_hash)
    witnesses = build_witnesses_doc(result.decision, graph_hash)
    Path(args.report).write_text(to_canonical_json(report), encoding="utf-8")
    Path(args.selection).write_text(to_canonical_json(selection), encoding="utf-8")
    Path(args.witnesses).write_text(to_canonical_json(witnesses), encoding="utf-8")
    print(_summary(result))
    return ExitCode.OK


def _run_analyze() -> int:
    snapshot = snapshot_working_tree(Path.cwd())
    graph = snapshot.graph
    health = {
        "files": len(snapshot.files),
        "nodes": graph.digraph.num_nodes(),
        "edges": graph.digraph.num_edges(),
        "tests": sum(1 for node in graph.nodes.values() if node.kind is NodeKind.TEST),
        "tainted": sum(1 for node in graph.nodes.values() if node.tainted),
        "roots": list(snapshot.index.roots),
        "graph_hash": graph.graph_hash,
    }
    print(to_canonical_json(health), end="")
    return ExitCode.OK


def _run_explain(args: argparse.Namespace) -> int:
    result = run_select(args.base, args.head, Path.cwd())
    lines, code = explain_lines(args.test, result)
    stream = sys.stdout if code is ExitCode.OK else sys.stderr
    for line in lines:
        print(line, file=stream)
    return code


def _run_replay(args: argparse.Namespace) -> int:
    lines, code = run_replay(Path(args.report), Path(args.witnesses), Path.cwd())
    stream = sys.stdout if code is ExitCode.OK else sys.stderr
    for line in lines:
        print(line, file=stream)
    return code


def _write_failure_docs(args: argparse.Namespace, error: Exception) -> None:
    finding = Finding(
        rule=RuleId.INTERNAL_ERROR,
        scope=Scope(kind=ScopeKind.GLOBAL),
        subject="acquit",
        reason=str(error) or type(error).__name__,
    )
    run = RunInfo(base_sha=args.base, head_sha=args.head, created_at=_now())
    report = build_run_all_report(run, findings=[finding])
    selection = build_selection(SelectionMode.RUN_ALL, skip=[], graph_hash=None)
    with suppress(OSError):
        Path(args.report).write_text(to_canonical_json(report), encoding="utf-8")
    with suppress(OSError):
        Path(args.selection).write_text(to_canonical_json(selection), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "select":
            return _run_select(args)
        if args.command == "analyze":
            return _run_analyze()
        if args.command == "explain":
            return _run_explain(args)
        return _run_replay(args)
    except Exception as error:  # fail closed on anything unexpected
        print(f"acquit: internal error, run all tests: {error}", file=sys.stderr)
        if args.command == "select":
            _write_failure_docs(args, error)
        return ExitCode.INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
