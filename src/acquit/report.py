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
    from acquit.select import Decision, SkippedTest
    from acquit.witness import Witness


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
            "blockers": [finding_to_dict(f) for f in findings],
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
            "blockers": [finding_to_dict(f) for f in result.blocking_findings],
            "waivers": [_waiver_to_dict(w) for w in result.outcome.waived],
        },
        "tests": {
            "selected": [
                {"path": entry.path, "reasons": list(entry.reasons)} for entry in decision.selected
            ],
            "skipped": [_skipped_to_dict(entry) for entry in decision.skipped],
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


def _skipped_to_dict(entry: SkippedTest) -> dict[str, Any]:
    # The flag only appears when set: non-narrowed entries stay byte-identical.
    document: dict[str, Any] = {"path": entry.path, "witness": entry.witness_id}
    if entry.narrowed:
        document["narrowed"] = True
    return document


def _witness_to_dict(witness: Witness) -> dict[str, Any]:
    document: dict[str, Any] = {
        "id": witness.id,
        "test": witness.test,
        "closure": witness.closure_hash,
        "changed": list(witness.changed),
        "claim": witness.claim,
    }
    if witness.narrowed:
        # The ADR 0008 evidence block; disjoint witnesses stay byte-identical.
        document["narrowed"] = [
            {
                "path": entry.path,
                "base_blob": entry.base_blob,
                "head_blob": entry.head_blob,
                "inits": [
                    {"path": init.path, "base_tier": init.base_tier, "head_tier": init.head_tier}
                    for init in entry.inits
                ],
                "region_count": entry.region_count,
                "region_hash": entry.region_hash,
            }
            for entry in witness.narrowed
        ]
    return document


def build_witnesses_doc(decision: Decision, graph_hash: str) -> dict[str, Any]:
    """Build the witnesses document: machine-checkable evidence for every skip."""
    return {
        "schema": WITNESSES_SCHEMA,
        "graph_hash": graph_hash,
        "closures": {key: list(paths) for key, paths in sorted(decision.closures.items())},
        "witnesses": [
            _witness_to_dict(witness)
            for witness in sorted(decision.witnesses, key=lambda witness: witness.id)
        ],
    }


def build_selection_doc(
    decision: Decision,
    graph_hash: str,
    head_sha: str | None,
    tree_fingerprint: str,
    artifacts: Mapping[str, str | None] | None = None,
    blockers: tuple[Finding, ...] = (),
    canary_changed: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The document the pytest plugin consumes. It lists provably skippable files
    bound to the analyzed tree; anything not listed runs, so unknown files run
    by default, and a tree that no longer matches the fingerprint runs everything.
    artifacts records where select wrote its three documents, repo-relative when
    they land inside the repo and null otherwise, so verifiers can exempt them
    from the tree fingerprint."""
    recorded: dict[str, str | None] = (
        {"report": None, "selection": None, "witnesses": None}
        if artifacts is None
        else dict(artifacts)
    )
    return {
        "schema": SELECTION_SCHEMA,
        "mode": str(decision.mode),
        "graph_hash": graph_hash,
        "tree": {"head_sha": head_sha, "fingerprint": tree_fingerprint},
        "artifacts": recorded,
        "skip": [{"path": entry.path, "witness": entry.witness_id} for entry in decision.skipped],
        # Diagnostics only; enforcement reads skip.
        "canary": {
            "selected": [
                {"path": entry.path, "reasons": list(entry.reasons)} for entry in decision.selected
            ],
            "always_run": [
                {"path": entry.path, "finding": entry.finding} for entry in decision.always_run
            ],
            "fallback": [finding_to_dict(finding) for finding in blockers],
            "changed": [] if canary_changed is None else canary_changed,
        },
    }


def build_run_all_selection() -> dict[str, Any]:
    """A selection that skips nothing and binds no tree: the safe default."""
    return {
        "schema": SELECTION_SCHEMA,
        "mode": str(SelectionMode.RUN_ALL),
        "graph_hash": None,
        "tree": {"head_sha": None, "fingerprint": None},
        "skip": [],
    }


def to_canonical_json(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, indent=2) + "\n"


@dataclass(frozen=True, slots=True)
class ReportDigest:
    """Headline numbers of a report document, for markdown renderers."""

    mode: str
    selected: int
    skipped: int
    always_run: int
    total: int
    estimated_seconds_saved: float | None


def _stat_count(stats: Mapping[str, Any], key: str) -> int:
    value = stats.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def digest_report(report: Mapping[str, Any]) -> ReportDigest:
    """Extract headline numbers from a report document, however minimal.

    Fail-closed fallback reports carry only a decision mode; every missing or
    malformed field degrades to zero (or None), and any mode other than a
    literal "selective" reads as run-all, mirroring the action's own sniffing.
    """
    decision = report.get("decision")
    mode_raw = decision.get("mode") if isinstance(decision, Mapping) else None
    selective = mode_raw == str(SelectionMode.SELECTIVE)
    mode = str(SelectionMode.SELECTIVE) if selective else str(SelectionMode.RUN_ALL)
    stats_raw = report.get("stats")
    stats: Mapping[str, Any] = stats_raw if isinstance(stats_raw, Mapping) else {}
    saved_raw = stats.get("estimated_seconds_saved")
    saved: float | None = None
    if isinstance(saved_raw, int | float) and not isinstance(saved_raw, bool):
        saved = float(saved_raw)
    return ReportDigest(
        mode=mode,
        selected=_stat_count(stats, "selected"),
        skipped=_stat_count(stats, "skipped"),
        always_run=_stat_count(stats, "always_run"),
        total=_stat_count(stats, "total"),
        estimated_seconds_saved=saved,
    )
