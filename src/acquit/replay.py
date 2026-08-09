"""Machine-checked replay of a select run.

Replay rebuilds the head snapshot from scratch (no cache), recomputes every
skipped test's import closure, and re-verifies every witness from first
principles. A report with narrowed witnesses (ADR 0008) additionally gets
its base snapshot rebuilt, also cache-free, and every narrowing condition
re-derived with the production checkers. This is what makes witnesses
evidence rather than logs.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acquit import vcs
from acquit.config import load_config
from acquit.constants import REPORT_SCHEMA, SELECTION_SCHEMA, WITNESSES_SCHEMA
from acquit.errors import ExitCode, GraphError, VcsError
from acquit.pipeline import Snapshot, snapshot_tree
from acquit.pytestmap.pytestcfg import load_pytest_config
from acquit.select import NarrowingContext, NarrowingJudge, NarrowingRefusal, import_closure
from acquit.vcs import ChangedFile
from acquit.witness import NarrowedFile, ReliedInit, Witness, closure_hash, verify_witness

Lines = tuple[str, ...]


def _load_document(path: Path, schema: str) -> dict[str, Any]:
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != schema:
        raise ValueError(f"{path} is not a {schema} document")
    return data


def _narrowed_from_entry(entry: dict[str, Any]) -> tuple[NarrowedFile, ...]:
    blocks = entry.get("narrowed", [])
    return tuple(
        NarrowedFile(
            path=item["path"],
            base_blob=item["base_blob"],
            head_blob=item["head_blob"],
            inits=tuple(
                ReliedInit(
                    path=init["path"],
                    base_tier=init["base_tier"],
                    head_tier=init["head_tier"],
                )
                for init in item["inits"]
            ),
            region_count=item["region_count"],
            region_hash=item["region_hash"],
        )
        for item in blocks
    )


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
                narrowed=_narrowed_from_entry(entry),
            )
        except (KeyError, TypeError) as error:
            failures.append(f"witnesses doc: malformed witness entry: {error!r}")
            continue
        out[witness.id] = witness
    return out


def _full_changed_paths(changed: tuple[ChangedFile, ...]) -> tuple[str, ...]:
    """The changed set decide() records into witnesses: head plus base paths."""
    paths = {change.path for change in changed}
    paths.update(change.old_path for change in changed if change.old_path is not None)
    return tuple(sorted(paths))


@dataclass(frozen=True, slots=True)
class _NarrowedReplay:
    """Both-snapshot re-derivation for narrowed witnesses (ADR 0008)."""

    judge: NarrowingJudge
    changed: tuple[str, ...]

    def check(self, path: str, witness: Witness, failures: list[str]) -> bool:
        if witness.changed != self.changed:
            failures.append(
                f"{path}: witness {witness.id} changed set does not match the diff "
                f"between the recorded commits"
            )
            return False
        outcome = self.judge.judge(witness.test)
        if isinstance(outcome, NarrowingRefusal):
            failures.append(
                f"{path}: witness {witness.id} narrowing does not re-derive: "
                f"{outcome.reason} ({outcome.subject})"
            )
            return False
        if tuple(entry.path for entry in outcome) != tuple(
            entry.path for entry in witness.narrowed
        ):
            failures.append(
                f"{path}: witness {witness.id} narrowed listing does not match "
                f"the recomputed intersection"
            )
            return False
        for recorded, derived in zip(witness.narrowed, outcome, strict=True):
            if (recorded.base_blob, recorded.head_blob) != (derived.base_blob, derived.head_blob):
                failures.append(
                    f"{path}: witness {witness.id} blob sha mismatch for {recorded.path}"
                )
                return False
            if recorded.inits != derived.inits:
                failures.append(
                    f"{path}: witness {witness.id} relied inits mismatch for {recorded.path} "
                    f"(condition 5)"
                )
                return False
            if (recorded.region_count, recorded.region_hash) != (
                derived.region_count,
                derived.region_hash,
            ):
                failures.append(
                    f"{path}: witness {witness.id} region accounting mismatch for "
                    f"{recorded.path} (condition 7)"
                )
                return False
        return True


def _check_skipped(
    entry: dict[str, Any],
    snapshot: Snapshot,
    witnesses: dict[str, Witness],
    closures: dict[str, Any],
    narrower: _NarrowedReplay | None,
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
    if witness.narrowed:
        if narrower is None:
            failures.append(
                f"{path}: witness {witness_id} is narrowed but no base snapshot is available"
            )
            return False
        return narrower.check(path, witness, failures)
    return True


def _skip_pairs(entries: Any, failures: list[str], source: str) -> list[tuple[str, str]] | None:
    if not isinstance(entries, list):
        failures.append(f"{source}: skip entries are not a list")
        return None
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("witness"), str)
        ):
            failures.append(f"{source}: malformed skip entry {entry!r}")
            return None
        pairs.append((entry["path"], entry["witness"]))
    return pairs


def _check_selection(
    selection: dict[str, Any], report: dict[str, Any], fresh_hash: str, failures: list[str]
) -> None:
    """Cross-check the document that actually deselects tests against the report."""
    if selection.get("graph_hash") != fresh_hash:
        failures.append(
            f"graph hash mismatch: rebuilt {fresh_hash}, selection doc says "
            f"{selection.get('graph_hash')}"
        )
    claimed = _skip_pairs(selection.get("skip"), failures, "selection doc")
    reported = _skip_pairs(report.get("tests", {}).get("skipped"), failures, "report")
    if claimed is None or reported is None:
        return
    for path, witness in sorted(set(claimed) - set(reported)):
        failures.append(f"selection doc skips {path} ({witness}) but the report does not")
    for path, witness in sorted(set(reported) - set(claimed)):
        failures.append(f"report skips {path} ({witness}) but the selection doc does not")


def run_replay(
    report_path: Path, witnesses_path: Path, selection_path: Path | None, cwd: Path
) -> tuple[Lines, ExitCode]:
    """Re-verify a report's witnesses against a fresh snapshot of its head sha."""
    try:
        report = _load_document(report_path, REPORT_SCHEMA)
        witnesses_doc = _load_document(witnesses_path, WITNESSES_SCHEMA)
        selection = (
            None if selection_path is None else _load_document(selection_path, SELECTION_SCHEMA)
        )
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
    acquit_config = load_config(repo)
    pytest_config = load_pytest_config(repo)
    snapshot = snapshot_tree(head_sha, repo, acquit_config, pytest_config, cache=None)

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

    if selection is not None:
        _check_selection(selection, report, fresh_hash, failures)

    witnesses = _witnesses_by_id(witnesses_doc, failures)
    closures = witnesses_doc.get("closures")
    if not isinstance(closures, dict):
        failures.append("witnesses doc: 'closures' is not an object")
        closures = {}

    # Narrowed witnesses need both commits: rebuild the base snapshot with no
    # cache and re-derive every condition from the trees, not the record.
    narrower: _NarrowedReplay | None = None
    if any(witness.narrowed for witness in witnesses.values()):
        base_sha = report.get("run", {}).get("base_sha")
        if not isinstance(base_sha, str):
            failures.append("narrowed witnesses need a base sha in the report run block")
        else:
            try:
                base_snapshot = snapshot_tree(
                    base_sha, repo, acquit_config, pytest_config, cache=None
                )
                changed = vcs.changed_files(base_sha, head_sha, repo)
            except VcsError as error:
                failures.append(f"narrowed witnesses: cannot rebuild the base commit: {error}")
            else:
                ctx = NarrowingContext(
                    head_facts=snapshot.facts,
                    base_facts=base_snapshot.facts,
                    head_blobs=snapshot.blob_shas,
                    base_blobs=base_snapshot.blob_shas,
                )
                narrower = _NarrowedReplay(
                    judge=NarrowingJudge(snapshot.graph, base_snapshot.graph, ctx, changed),
                    changed=_full_changed_paths(changed),
                )

    verified = 0
    for entry in report.get("tests", {}).get("skipped", []):
        if _check_skipped(entry, snapshot, witnesses, closures, narrower, failures):
            verified += 1

    if failures:
        return (tuple(failures), ExitCode.REPLAY_MISMATCH)
    return ((f"replay ok: {verified} witnesses verified",), ExitCode.OK)
