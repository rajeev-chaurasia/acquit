"""Report and selection documents, with canonical serialization.

The determinism contract: identical inputs produce byte-identical documents,
except the created_at field, which callers inject and hashes exclude.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from acquit import __version__
from acquit.constants import REPORT_SCHEMA, SELECTION_SCHEMA
from acquit.policy.model import Finding


class SelectionMode(StrEnum):
    RUN_ALL = "run-all"
    SELECTIVE = "selective"


@dataclass(frozen=True, slots=True)
class RunInfo:
    base_sha: str | None
    head_sha: str | None
    created_at: str


def finding_to_dict(finding: Finding) -> dict[str, Any]:
    return {
        "rule": str(finding.rule),
        "scope": str(finding.scope.kind),
        "subject": finding.subject,
        "reason": finding.reason,
    }


def build_run_all_report(run: RunInfo, findings: list[Finding]) -> dict[str, Any]:
    """A report that selects nothing and skips nothing: the safe default."""
    return {
        "schema": REPORT_SCHEMA,
        "tool": {"name": "acquit", "version": __version__},
        "run": {
            "base_sha": run.base_sha,
            "head_sha": run.head_sha,
            "created_at": run.created_at,
        },
        "decision": {
            "mode": str(SelectionMode.RUN_ALL),
            "findings": [finding_to_dict(f) for f in findings],
            "waivers": [],
        },
        "tests": {"selected": [], "skipped": [], "always_run": []},
        "stats": {"selected": 0, "skipped": 0, "total": 0},
    }


def build_selection(mode: SelectionMode, skip: list[str], graph_hash: str | None) -> dict[str, Any]:
    """The document the pytest plugin consumes. It lists provably skippable files;
    anything not listed runs, so unknown files run by default."""
    return {
        "schema": SELECTION_SCHEMA,
        "mode": str(mode),
        "skip": sorted(skip),
        "graph_hash": graph_hash,
    }


def to_canonical_json(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, indent=2) + "\n"
