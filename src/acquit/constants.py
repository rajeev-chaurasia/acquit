"""Shared constants. Values that appear in more than one module live here."""

from typing import Final

REPORT_SCHEMA: Final = "acquit/report-v1"
SELECTION_SCHEMA: Final = "acquit/selection-v2"
WITNESSES_SCHEMA: Final = "acquit/witnesses-v1"
CANARY_SCHEMA: Final = "acquit/canary-v1"

ENV_SELECTION_FILE: Final = "ACQUIT_SELECTION_FILE"
ENV_CANARY: Final = "ACQUIT_CANARY"
ENV_CACHE_DIR: Final = "ACQUIT_CACHE_DIR"

DEFAULT_REPORT_FILE: Final = "acquit-report.json"
DEFAULT_SELECTION_FILE: Final = "acquit-selection.json"
DEFAULT_WITNESSES_FILE: Final = "acquit-witnesses.json"

COMMENT_MARKER: Final = "<!-- acquit-report -->"

# Refuse selection documents larger than this before reading them.
SELECTION_SIZE_CAP: Final = 5 * 1024 * 1024
