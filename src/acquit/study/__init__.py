"""The replay-study harness: sample merged PRs, re-run them, aggregate evidence.

The study is a test of acquit's headline claim: across N historical merged
PRs, some share of test time is safely skippable with zero unsafe skips.
Schema identifiers shared by more than one study module live here, mirroring
acquit.constants for the main package.
"""

from typing import Final

MANIFEST_SCHEMA: Final = "acquit/study-manifest-v1"
RESULT_SCHEMA: Final = "acquit/study-result-v1"
EXCLUSION_SCHEMA: Final = "acquit/study-exclusion-v1"
SUMMARY_SCHEMA: Final = "acquit/study-summary-v1"
