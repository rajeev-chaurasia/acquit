"""GITHUB_OUTPUT and GITHUB_STEP_SUMMARY writers for the composite action.

The same heredoc discipline as the action's bash steps: every output is
written in NAME<<DELIMITER form with the action's fixed delimiter, and any
value that could break out of that form (a newline, or the delimiter itself)
is refused before a single byte lands in the runner file. Like the comment,
this is delivery plumbing: every failure is a warning on stderr and a zero
exit, and downstream steps that read empty outputs fail closed on their own.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from acquit.errors import AcquitError, ExitCode
from acquit.report import ReportDigest, SelectionMode, digest_report

# Must match the delimiter in action.yml, so the two writers stay
# interchangeable to anything parsing the runner files.
DELIMITER: Final = "EOF_ACQUIT_c9d41e"

_NAME_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")


def format_output_line(name: str, value: str) -> str:
    """One GITHUB_OUTPUT record in heredoc form; hostile values are refused."""
    if not _NAME_PATTERN.fullmatch(name):
        raise AcquitError(f"refusing output name {name!r}")
    if "\n" in value or "\r" in value:
        raise AcquitError(f"refusing output {name!r}: value contains a newline")
    if DELIMITER in value:
        raise AcquitError(f"refusing output {name!r}: value contains the heredoc delimiter")
    return f"{name}<<{DELIMITER}\n{value}\n{DELIMITER}\n"


def _load(path: Path) -> dict[str, Any]:
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AcquitError(f"{path}: expected a json object")
    return document


def _summary_markdown(mode: str, digest: ReportDigest) -> str:
    if mode == str(SelectionMode.SELECTIVE):
        detail = (
            f"{digest.skipped} of {digest.total} test files provably unaffected; "
            f"{digest.selected} selected, {digest.always_run} always-run."
        )
    else:
        detail = "Every test file runs."
    return f"### Acquit decision\n\nMode: `{mode}`\n\n{detail}\n"


def run_ci_outputs(
    report_path: Path, selection_path: Path, env: Mapping[str, str] | None = None
) -> int:
    """The `acquit ci-outputs` command body. Never fails: plumbing is best effort."""
    environment = os.environ if env is None else env
    try:
        # The paths are the only caller-controlled values; refuse hostile ones
        # before they are even opened.
        path_lines = format_output_line("report", str(report_path)) + format_output_line(
            "selection", str(selection_path)
        )
        report = _load(report_path)
        selection = _load(selection_path)
        digest = digest_report(report)
        # The selection is the document pytest obeys; when replay rewrote it,
        # its mode wins over whatever the report still claims.
        selective = selection.get("mode") == str(SelectionMode.SELECTIVE)
        mode = str(SelectionMode.SELECTIVE) if selective else str(SelectionMode.RUN_ALL)
        counts = {
            "mode": mode,
            "selected": str(digest.selected),
            "skipped": str(digest.skipped),
            "always-run": str(digest.always_run),
            "total": str(digest.total),
        }
        # Format everything before writing anything: a refusal must never
        # leave a half-written record set behind.
        lines = "".join(format_output_line(name, value) for name, value in counts.items())
        lines += path_lines
        output_file = environment.get("GITHUB_OUTPUT", "")
        if not output_file:
            raise AcquitError("GITHUB_OUTPUT is not set")
        with Path(output_file).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(lines)
        summary_file = environment.get("GITHUB_STEP_SUMMARY", "")
        if summary_file:
            with Path(summary_file).open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(_summary_markdown(mode, digest))
    except Exception as error:
        print(f"acquit: warning: ci outputs skipped: {error}", file=sys.stderr)
    return ExitCode.OK
