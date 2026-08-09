"""Command line entry point.

Every failure path converges on a run-all report. The tool may only be wrong
in the safe direction.
"""

import argparse
import json
import stat
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from acquit import __version__, vcs
from acquit.constants import (
    DEFAULT_REPORT_FILE,
    DEFAULT_SELECTION_FILE,
    DEFAULT_WITNESSES_FILE,
)
from acquit.errors import AcquitError, ExitCode, VcsError
from acquit.explain import explain_lines
from acquit.gh.comment import run_comment
from acquit.gh.outputs import run_ci_outputs
from acquit.graph.model import NodeKind
from acquit.pipeline import SelectResult, run_select, snapshot_working_tree
from acquit.policy.model import Finding, RuleId, Scope, ScopeKind
from acquit.replay import run_replay
from acquit.report import (
    RunInfo,
    SelectionMode,
    build_report,
    build_run_all_report,
    build_run_all_selection,
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
    replay.add_argument(
        "--witnesses",
        help=f"witnesses file path, defaults to {DEFAULT_WITNESSES_FILE} beside the report",
    )
    replay.add_argument(
        "--selection",
        help=(
            "selection file to cross-check against the report, defaults to "
            f"{DEFAULT_SELECTION_FILE} beside the report when present"
        ),
    )

    comment = subcommands.add_parser(
        "comment", help="post or update the sticky PR comment for a report; never fails CI"
    )
    comment.add_argument("report", help="path to an acquit report file")
    comment.add_argument("--pr", type=int, help="pull request number; otherwise from GITHUB_REF")

    # A separate subcommand rather than a comment flag: the action calls it on
    # every run, comment or not, and its contract (runner files) is different.
    ci_outputs = subcommands.add_parser(
        "ci-outputs", help="write GitHub action outputs and a step summary; never fails CI"
    )
    ci_outputs.add_argument("report", help="path to an acquit report file")
    ci_outputs.add_argument("selection", help="path to the selection file pytest will obey")
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


def _write_document(path: Path, document: dict[str, Any]) -> None:
    """Write one document atomically: temp file in place, then replace."""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(to_canonical_json(document), encoding="utf-8")
        try:
            tmp.replace(path)
        except PermissionError:
            # Windows refuses to replace a read-only target; clear the bit.
            path.chmod(stat.S_IWRITE)
            tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _artifact_paths(repo: Path, args: argparse.Namespace) -> dict[str, str | None]:
    """Repo-relative posix paths of the three output documents, None when outside."""
    resolved_repo = repo.resolve()
    out: dict[str, str | None] = {}
    named = (("report", args.report), ("selection", args.selection), ("witnesses", args.witnesses))
    for key, raw in named:
        try:
            target = (Path.cwd() / raw).resolve()
        except OSError:
            out[key] = None
            continue
        out[key] = (
            target.relative_to(resolved_repo).as_posix()
            if target.is_relative_to(resolved_repo)
            else None
        )
    return out


def _run_select(args: argparse.Namespace) -> int:
    created_at = _now()
    durations = _load_durations(args.durations)
    artifacts = _artifact_paths(vcs.repo_root(Path.cwd()), args)
    # Select's own outputs are exempt from the working-tree fingerprint on both
    # sides. Sound: excluding acquit's freshly written documents cannot hide a
    # user change, and once committed they are tracked diff content covered by
    # R001 like any other resource.
    exclude = frozenset(path for path in artifacts.values() if path is not None)
    result = run_select(args.base, args.head, Path.cwd(), exclude)
    graph_hash = result.head.graph.graph_hash
    report = build_report(result, created_at=created_at, durations=durations)
    selection = build_selection_doc(
        result.decision, graph_hash, result.head_sha, result.tree_fingerprint, artifacts
    )
    witnesses = build_witnesses_doc(result.decision, graph_hash)
    _write_document(Path(args.report), report)
    _write_document(Path(args.selection), selection)
    _write_document(Path(args.witnesses), witnesses)
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
    report = Path(args.report)
    # Omitted document flags resolve beside the report, never against the cwd.
    witnesses = (
        report.with_name(DEFAULT_WITNESSES_FILE) if args.witnesses is None else Path(args.witnesses)
    )
    selection: Path | None = None if args.selection is None else Path(args.selection)
    if selection is None:
        sibling = report.with_name(DEFAULT_SELECTION_FILE)
        selection = sibling if sibling.is_file() else None
    lines, code = run_replay(report, witnesses, selection, Path.cwd())
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
    # The selection is what pytest obeys, so it must convert to run-all first,
    # and a failure to convert it must be loud, never suppressed.
    for path, document in ((args.selection, build_run_all_selection()), (args.report, report)):
        try:
            _write_document(Path(path), document)
        except Exception as write_error:
            print(
                f"acquit: FAILED to write fail-closed document {path}: {write_error}",
                file=sys.stderr,
            )


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
        if args.command == "comment":
            return run_comment(Path(args.report), args.pr)
        if args.command == "ci-outputs":
            return run_ci_outputs(Path(args.report), Path(args.selection))
        return _run_replay(args)
    except Exception as error:  # fail closed on anything unexpected
        if args.command in ("comment", "ci-outputs"):
            # Delivery must never fail CI, even if its own guard rails break.
            print(f"acquit: warning: {args.command} skipped: {error}", file=sys.stderr)
            return ExitCode.OK
        if args.command == "select" and isinstance(error, VcsError):
            # A bad ref is an operator mistake, not a tool bug; say so plainly.
            detail = str(error).splitlines()[0] if str(error) else type(error).__name__
            print(
                f"acquit: cannot resolve the requested refs, running all tests: {detail}",
                file=sys.stderr,
            )
        else:
            print(f"acquit: internal error, run all tests: {error}", file=sys.stderr)
        if args.command == "select":
            _write_failure_docs(args, error)
        return ExitCode.INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
