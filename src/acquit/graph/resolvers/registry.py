"""Registry of resolver residents, in evaluation order.

Re-export narrowing is the first resident; per the census ranking, dynamic
import constant folding is the intended second. The tuple type widens to
the Resolver protocol once a second resident lands and the builder learns
to dispatch on bound types.
"""

from typing import Final

from acquit.graph.resolvers.framework import Resolver
from acquit.graph.resolvers.reexport import PureInit, ReexportCandidate, ReexportResolver

REGISTRY: Final[tuple[ReexportResolver, ...]] = (ReexportResolver(),)

# mypy proves the resident satisfies the seam's protocol.
_CONFORMS: Resolver[ReexportCandidate, PureInit] = REGISTRY[0]
