"""Command line entry point.

Every failure path converges on a run-all report. The tool may only be wrong
in the safe direction.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from acquit import __version__
from acquit.constants import DEFAULT_REPORT_FILE, DEFAULT_SELECTION_FILE
from acquit.errors import ExitCode
from acquit.policy.model import Finding, RuleId, Scope, ScopeKind
from acquit.report import (
    RunInfo,
    SelectionMode,
    build_run_all_report,
    build_selection,
    to_canonical_json,
)

NOT_IMPLEMENTED_FINDING = Finding(
    rule=RuleId.INTERNAL_ERROR,
    scope=Scope(kind=ScopeKind.GLOBAL),
    subject="acquit",
    reason="The analysis engine is not implemented yet. Running the full suite.",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acquit")
    parser.add_argument("--version", action="version", version=f"acquit {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    select = subcommands.add_parser("select", help="decide which tests must run for a diff")
    select.add_argument("--base", help="base git ref")
    select.add_argument("--head", help="head git ref, defaults to the working tree")
    select.add_argument("--report", default=DEFAULT_REPORT_FILE, help="report output path")
    select.add_argument("--selection", default=DEFAULT_SELECTION_FILE, help="selection output path")

    subcommands.add_parser("analyze", help="build the dependency graph and print its health")
    return parser


def _run_select(args: argparse.Namespace) -> int:
    run = RunInfo(
        base_sha=args.base,
        head_sha=args.head,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    report = build_run_all_report(run, findings=[NOT_IMPLEMENTED_FINDING])
    selection = build_selection(SelectionMode.RUN_ALL, skip=[], graph_hash=None)
    Path(args.report).write_text(to_canonical_json(report), encoding="utf-8")
    Path(args.selection).write_text(to_canonical_json(selection), encoding="utf-8")
    print(f"acquit: run-all ({NOT_IMPLEMENTED_FINDING.reason})")
    return ExitCode.OK


def _run_analyze() -> int:
    print("acquit: graph analysis is not implemented yet")
    return ExitCode.OK


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "select":
            return _run_select(args)
        return _run_analyze()
    except Exception as error:  # fail closed on anything unexpected
        print(f"acquit: internal error, run all tests: {error}", file=sys.stderr)
        return ExitCode.INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
