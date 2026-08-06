"""Skip witnesses: the evidence behind every skipped test.

A test may only leave the run by presenting a Witness, and a Witness can only
be constructed while the disjointness claim actually holds. Verification
recomputes everything from first principles so replay never has to trust the
run that produced the witness.
"""

import hashlib
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

from acquit.errors import PolicyError

CLAIM_DISJOINT: Final = "closure(test) does not intersect changed set"


@dataclass(frozen=True, slots=True)
class Witness:
    """Evidence that one test's import closure avoids every changed path.

    Ids are "w-000001" style, numbered in sorted skipped-test order.
    """

    id: str
    test: str
    closure_hash: str
    changed: tuple[str, ...]
    claim: str


def closure_hash(closure: Collection[str]) -> str:
    """Hash the canonical closure listing: sorted paths joined by newlines."""
    return hashlib.sha256("\n".join(sorted(closure)).encode("utf-8")).hexdigest()


def build_witness(
    index: int, test: str, closure: Collection[str], changed: Collection[str]
) -> Witness:
    """Construct the witness for one skipped test, re-verifying disjointness.

    Raises PolicyError when the closure intersects the changed set: a witness
    that cannot be honestly constructed must never exist.
    """
    overlap = sorted(set(closure) & set(changed))
    if overlap:
        raise PolicyError(f"witness refused for {test}: closure intersects changes {overlap}")
    return Witness(
        id=f"w-{index:06d}",
        test=test,
        closure_hash=closure_hash(closure),
        changed=tuple(sorted(set(changed))),
        claim=CLAIM_DISJOINT,
    )


def verify_witness(w: Witness, closure: Collection[str], changed: Collection[str]) -> bool:
    """Recheck a witness from first principles; used by replay."""
    return (
        w.claim == CLAIM_DISJOINT
        and w.closure_hash == closure_hash(closure)
        and w.changed == tuple(sorted(set(changed)))
        and not set(closure) & set(changed)
    )
