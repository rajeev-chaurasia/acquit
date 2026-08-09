"""Skip witnesses: the evidence behind every skipped test.

A test may only leave the run by presenting a Witness, and a Witness can only
be constructed while its claim actually holds: disjointness in the classic
form, or the ADR 0008 narrowed claim when every intersecting file carries the
per-file narrowing evidence. Verification recomputes everything from first
principles so replay never has to trust the run that produced the witness.
"""

import hashlib
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

from acquit.errors import PolicyError

CLAIM_DISJOINT: Final = "closure(test) does not intersect changed set"
# The ADR 0008 claim, verbatim. A witness carrying it must list one narrowed
# entry per intersecting file; replay re-derives all six conditions.
CLAIM_NARROWED: Final = (
    "closure(test) intersects changed set only in import-time-only files, "
    "each modified in place and import-inert across base and head"
)


@dataclass(frozen=True, slots=True)
class ReliedInit:
    """One pure re-exporter init the narrowing crossed, with its proven tiers."""

    path: str
    base_tier: str
    head_tier: str


@dataclass(frozen=True, slots=True)
class NarrowedFile:
    """The evidence excusing one intersecting file as import-time-only."""

    path: str
    base_blob: str
    head_blob: str
    inits: tuple[ReliedInit, ...]


@dataclass(frozen=True, slots=True)
class Witness:
    """Evidence that one test's import closure avoids every changed path, or
    (narrowed form) intersects it only in proven import-time-only files.

    Ids are "w-000001" style, numbered in sorted skipped-test order.
    """

    id: str
    test: str
    closure_hash: str
    changed: tuple[str, ...]
    claim: str
    # Nonempty exactly when the claim is CLAIM_NARROWED: one entry per
    # intersecting file, in sorted path order (ADR 0008).
    narrowed: tuple[NarrowedFile, ...] = ()


def closure_hash(closure: Collection[str]) -> str:
    """Hash the canonical closure listing: sorted paths joined by newlines."""
    return hashlib.sha256("\n".join(sorted(closure)).encode("utf-8")).hexdigest()


def build_witness(
    index: int,
    test: str,
    closure: Collection[str],
    changed: Collection[str],
    narrowed: tuple[NarrowedFile, ...] = (),
) -> Witness:
    """Construct the witness for one skipped test, re-verifying its claim.

    Without a narrowed block the closure must be disjoint from the changed
    set. With one, the block must list the (necessarily nonempty)
    intersection exactly, and every entry must name the inits it relied on.
    Raises PolicyError otherwise: a witness that cannot be honestly
    constructed must never exist.
    """
    overlap = sorted(set(closure) & set(changed))
    if not narrowed:
        if overlap:
            raise PolicyError(f"witness refused for {test}: closure intersects changes {overlap}")
        claim = CLAIM_DISJOINT
    else:
        listed = [entry.path for entry in narrowed]
        if listed != overlap:
            raise PolicyError(
                f"witness refused for {test}: narrowed block lists {listed} "
                f"but the intersection is {overlap}"
            )
        if any(not entry.inits for entry in narrowed):
            raise PolicyError(f"witness refused for {test}: a narrowed file relies on no init")
        claim = CLAIM_NARROWED
    return Witness(
        id=f"w-{index:06d}",
        test=test,
        closure_hash=closure_hash(closure),
        changed=tuple(sorted(set(changed))),
        claim=claim,
        narrowed=narrowed,
    )


def verify_witness(w: Witness, closure: Collection[str], changed: Collection[str]) -> bool:
    """Recheck a witness from first principles; used by replay.

    This checks the claim's set relations and the narrowed block's internal
    consistency; the block's six conditions themselves are re-derived by
    replay against fresh base and head snapshots.
    """
    if w.closure_hash != closure_hash(closure) or w.changed != tuple(sorted(set(changed))):
        return False
    overlap = sorted(set(closure) & set(changed))
    if w.narrowed:
        return (
            w.claim == CLAIM_NARROWED
            and [entry.path for entry in w.narrowed] == overlap
            and all(entry.inits for entry in w.narrowed)
        )
    return w.claim == CLAIM_DISJOINT and not overlap
