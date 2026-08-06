"""Machine-checked replay of a select run.

Replay rebuilds the head snapshot from scratch (no cache), recomputes every
skipped test's import closure, and re-verifies every witness from first
principles. This is what makes witnesses evidence rather than logs.
"""

import json
from pathlib import Path
from typing import Any

from acquit import vcs
from acquit.config import load_config
from acquit.constants import REPORT_SCHEMA, WITNESSES_SCHEMA
from acquit.errors import ExitCode, GraphError
from acquit.pipeline import Snapshot, snapshot_tree
from acquit.pytestmap.pytestcfg import load_pytest_config
from acquit.select import import_closure
from acquit.witness import Witness, closure_hash, verify_witness

Lines = tuple[str, ...]


def _load_document(path: Path, schema: str) -> dict[str, Any]:
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != schema:
        raise ValueError(f"{path} is not a {schema} document")
    return data


def _witnesses_by_id(doc: dict[str, Any], failures: list[str]) -> dict[str, Witness]:
    out: dict[str, Witness] = {}
    entries = doc.get("witnesses")
    if not isinstance(entries, list):
        failures.append("witnesses doc: 'witnesses' is not a list")
        return out
    for entry in entries:
        try:
            witness = Witness(
                id=entry["id"],
                test=entry["test"],
                closure_hash=entry["closure"],
                changed=tuple(entry["changed"]),
                claim=entry["claim"],
            )
        except (KeyError, TypeError) as error:
            failures.append(f"witnesses doc: malformed witness entry: {error!r}")
            continue
        out[witness.id] = witness
    return out


def _check_skipped(
    entry: dict[str, Any],
    snapshot: Snapshot,
    witnesses: dict[str, Witness],
    closures: dict[str, Any],
    failures: list[str],
) -> bool:
    path, witness_id = entry["path"], entry["witness"]
    witness = witnesses.get(witness_id)
    if witness is None:
        failures.append(f"{path}: witness {witness_id} is missing from the witnesses doc")
        return False
    if witness.test != path:
        failures.append(f"{path}: witness {witness_id} testifies for {witness.test}, not {path}")
        return False
    try:
        closure = import_closure(snapshot.graph, path)
    except GraphError as error:
        failures.append(f"{path}: {error}")
        return False
    recomputed = closure_hash(closure)
    if recomputed != witness.closure_hash:
        failures.append(
            f"{path}: closure hash mismatch: recomputed {recomputed}, "
            f"witness claims {witness.closure_hash}"
        )
        return False
    listing = closures.get(witness.closure_hash)
    if not isinstance(listing, list) or tuple(listing) != tuple(sorted(closure)):
        failures.append(f"{path}: closures entry for {witness.closure_hash} does not match")
        return False
    if not verify_witness(witness, closure, witness.changed):
        failures.append(f"{path}: witness {witness_id} failed verification")
        return False
    return True


def run_replay(report_path: Path, witnesses_path: Path, cwd: Path) -> tuple[Lines, ExitCode]:
    """Re-verify a report's witnesses against a fresh snapshot of its head sha."""
    try:
        report = _load_document(report_path, REPORT_SCHEMA)
        witnesses_doc = _load_document(witnesses_path, WITNESSES_SCHEMA)
    except (OSError, ValueError) as error:
        return ((f"acquit replay: {error}",), ExitCode.USAGE)

    head_sha = report.get("run", {}).get("head_sha")
    if not isinstance(head_sha, str):
        message = (
            "acquit replay: the report has no head sha (it was built from a "
            "working tree); replay needs a commit"
        )
        return ((message,), ExitCode.USAGE)

    repo = vcs.repo_root(cwd)
    snapshot = snapshot_tree(
        head_sha, repo, load_config(repo), load_pytest_config(repo), cache=None
    )

    failures: list[str] = []
    fresh_hash = snapshot.graph.graph_hash
    if fresh_hash != report.get("graph", {}).get("hash"):
        failures.append(
            f"graph hash mismatch: rebuilt {fresh_hash}, report says "
            f"{report.get('graph', {}).get('hash')}"
        )
    if fresh_hash != witnesses_doc.get("graph_hash"):
        failures.append(
            f"graph hash mismatch: rebuilt {fresh_hash}, witnesses doc says "
            f"{witnesses_doc.get('graph_hash')}"
        )

    witnesses = _witnesses_by_id(witnesses_doc, failures)
    closures = witnesses_doc.get("closures")
    if not isinstance(closures, dict):
        failures.append("witnesses doc: 'closures' is not an object")
        closures = {}

    verified = 0
    for entry in report.get("tests", {}).get("skipped", []):
        if _check_skipped(entry, snapshot, witnesses, closures, failures):
            verified += 1

    if failures:
        return (tuple(failures), ExitCode.REPLAY_MISMATCH)
    return ((f"replay ok: {verified} witnesses verified",), ExitCode.OK)
