"""Registry of resolver residents, in evaluation order.

Two residents, one on each side of the seam's axis (framework.py). Re-export
narrowing proves a relational, selection-time claim: base and head must
agree, and replay re-verifies the witness against two snapshots. Dynamic
import constant folding proves a per-revision, graph-construction claim: a
pure function of one module's bytes, consumed while edges are laid, that
replay re-derives when it rebuilds the snapshot with no extra evidence.
"""

from typing import Final

from acquit.graph.resolvers.folding import FOLDING_RESOLVER, FoldingResolver
from acquit.graph.resolvers.framework import Resolver
from acquit.graph.resolvers.reexport import PureInit, ReexportCandidate, ReexportResolver

# Build-time residents the graph assembler runs over module facts. The tuple
# type widens to the Resolver protocol once the builder dispatches on bound
# types.
REGISTRY: Final[tuple[ReexportResolver, ...]] = (ReexportResolver(),)

# The second resident lives on the per-revision side of the axis, so it runs
# at fact extraction, not in the build-time loop above: parse.py invokes the
# same instance directly (it cannot import this module without a cycle
# through framework.py), and this registration names it as a resident.
FOLDING: Final[FoldingResolver] = FOLDING_RESOLVER

# mypy proves the relational resident satisfies the seam's protocol.
_CONFORMS: Resolver[ReexportCandidate, PureInit] = REGISTRY[0]
