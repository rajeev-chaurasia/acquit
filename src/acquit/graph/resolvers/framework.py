"""The resolver seam: recognize a hazard idiom, then prove a bound or decline.

A resolver narrows one of the rule table's over-approximations. recognize
pattern-matches a hazard site; prove either returns a bound whose claim can
be re-derived by replay from the recorded revisions alone, or declines. A
decline always reproduces today's fail-closed behavior, so a resolver can
only ever add precision, never risk. Re-derivability is what separates a
resolver from a heuristic.

Every resident declares which side of the seam's axis it proves on, so
replay knows what each one owes it:

- "per-revision": the proof holds within one revision's graph construction
  and re-derives from that revision's blobs alone (ADR 0009 folding).
- "relational": the proof relates base and head, carries witness evidence,
  and replay re-verifies it against two snapshots (ADR 0008 narrowing).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Protocol, TypeVar

from acquit.graph.index import ModuleIndex
from acquit.graph.parse import ModuleFacts


@dataclass(frozen=True, slots=True)
class HazardSite:
    """One module a resolver may recognize as a narrowable idiom."""

    path: str
    facts: ModuleFacts


@dataclass(frozen=True, slots=True)
class ResolveContext:
    """Everything a proof may consult: the index and every module's facts."""

    index: ModuleIndex
    facts: Mapping[str, ModuleFacts]


@dataclass(frozen=True, slots=True)
class Decline:
    """A refused proof; declining keeps today's behavior for the site."""

    reason: str


CandidateT = TypeVar("CandidateT")
BoundT_co = TypeVar("BoundT_co", covariant=True)


class Resolver(Protocol[CandidateT, BoundT_co]):
    """One sound narrowing: recognize the idiom, prove a bound or decline."""

    axis: ClassVar[str]
    """Which side of the seam's axis the proof lives on; see module docstring."""

    def recognize(self, site: HazardSite) -> CandidateT | None:
        """Match one hazard site, returning the proof input or None."""
        ...

    def prove(self, candidate: CandidateT, ctx: ResolveContext) -> BoundT_co | Decline:
        """Prove a bound for the candidate, or decline fail-closed."""
        ...
