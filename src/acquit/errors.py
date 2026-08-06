"""Error taxonomy and process exit codes.

User-facing failures must degrade to a fail-closed report, never a bare traceback.
The CLI catches AcquitError (and everything else) and converts it to a run-all decision.
"""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    USAGE = 2
    INTERNAL = 3
    REPLAY_MISMATCH = 4


class AcquitError(Exception):
    """Base for all errors raised by acquit."""


class GraphError(AcquitError):
    """The dependency graph could not be built."""


class ResolutionError(AcquitError):
    """An import statement could not be resolved."""


class PolicyError(AcquitError):
    """The policy engine could not evaluate its rules."""


class VcsError(AcquitError):
    """Git information (refs, diffs, blobs) could not be obtained."""
