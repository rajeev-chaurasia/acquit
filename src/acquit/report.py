"""Report, selection, and witness documents, with canonical serialization.

The determinism contract: identical inputs produce byte-identical documents,
except the created_at field, which callers inject and hashes exclude.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from acquit import __version__
from acquit.constants import REPORT_SCHEMA, SELECTION_SCHEMA, WITNESSES_SCHEMA
from acquit.graph.model import GRAPH_SCHEMA_VERSION

if TYPE_CHECKING:
    from acquit.pipeline import SelectResult
    from acquit.policy.engine import WaivedFinding
    from acquit.policy.model import Finding
    from acquit.select import Decision


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


def _tool() -> dict[str, Any]:
    return {"name": "acquit", "version": __version__, "graph_schema": GRAPH_SCHEMA_VERSION}


def _waiver_to_dict(waived: WaivedFinding) -> dict[str, Any]:
    return {
        "rule": waived.waiver.rule,
        "glob": waived.waiver.glob,
        "justification": waived.waiver.justification,
        "subject": waived.finding.subject,
    }


def _empty_stats() -> dict[str, Any]:
    return {
        "selected": 0,
        "skipped": 0,
        "always_run": 0,
        "total": 0,
        "estimated_seconds_saved": None,
        "durations_source": None,
    }


def build_run_all_report(run: RunInfo, findings: list[Finding]) -> dict[str, Any]:
    """A report that selects nothing and skips nothing: the safe default."""
    return {
        "schema": REPORT_SCHEMA,
        "tool": _tool(),
        "run": {
            "base_sha": run.base_sha,
            "head_sha": run.head_sha,
            "created_at": run.created_at,
        },
        "graph": {"hash": None, "nodes": 0, "edges": 0, "roots": []},
        "changed": [],
        "decision": {
            "mode": str(SelectionMode.RUN_ALL),
            "findings": [finding_to_dict(f) for f in findings],
            "waivers": [],
        },
        "tests": {"selected": [], "skipped": [], "always_run": []},
        "stats": _empty_stats(),
    }


def build_report(
    result: SelectResult, created_at: str, durations: Mapping[str, float] | None
) -> dict[str, Any]:
    """Build the full report document for one completed selection run."""
    decision = result.decision
    graph = result.head.graph
    skipped_paths = [entry.path for entry in decision.skipped]
    saved = None if durations is None else sum(durations.get(path, 0.0) for path in skipped_paths)
    changed = sorted(
        (
            {
                "path": change.path,
                "kind": str(result.changed_kinds[change.path]),
                "status": str(change.status),
            }
            for change in result.changed
        ),
        key=lambda entry: (entry["path"], entry["status"]),
    )
    return {
        "schema": REPORT_SCHEMA,
        "tool": _tool(),
        "run": {
            "base_sha": result.base_sha,
            "head_sha": result.head_sha,
            "created_at": created_at,
        },
        "graph": {
            "hash": graph.graph_hash,
            "nodes": graph.digraph.num_nodes(),
            "edges": graph.digraph.num_edges(),
            "roots": list(result.head.index.roots),
        },
        "changed": changed,
        "decision": {
            "mode": str(decision.mode),
            "findings": [finding_to_dict(f) for f in result.outcome.findings],
            "waivers": [_waiver_to_dict(w) for w in result.outcome.waived],
        },
        "tests": {
            "selected": [
                {"path": entry.path, "reasons": list(entry.reasons)} for entry in decision.selected
            ],
            "skipped": [
                {"path": entry.path, "witness": entry.witness_id} for entry in decision.skipped
            ],
            "always_run": [
                {"path": entry.path, "finding": entry.finding} for entry in decision.always_run
            ],
        },
        "stats": {
            "selected": len(decision.selected),
            "skipped": len(decision.skipped),
            "always_run": len(decision.always_run),
            "total": len(decision.selected) + len(decision.skipped) + len(decision.always_run),
            "estimated_seconds_saved": saved,
            "durations_source": None if durations is None else "durations-file",
        },
    }


def build_witnesses_doc(decision: Decision, graph_hash: str) -> dict[str, Any]:
    """Build the witnesses document: machine-checkable evidence for every skip."""
    return {
        "schema": WITNESSES_SCHEMA,
        "graph_hash": graph_hash,
        "closures": {key: list(paths) for key, paths in sorted(decision.closures.items())},
        "witnesses": [
            {
                "id": witness.id,
                "test": witness.test,
                "closure": witness.closure_hash,
                "changed": list(witness.changed),
                "claim": witness.claim,
            }
            for witness in sorted(decision.witnesses, key=lambda witness: witness.id)
        ],
    }


def build_selection_doc(decision: Decision, graph_hash: str) -> dict[str, Any]:
    """Build the selection document the pytest plugin consumes for a decision."""
    return build_selection(decision.mode, [entry.path for entry in decision.skipped], graph_hash)


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
