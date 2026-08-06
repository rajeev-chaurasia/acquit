"""Shared constants. Values that appear in more than one module live here."""

from typing import Final

REPORT_SCHEMA: Final = "acquit/report-v1"
SELECTION_SCHEMA: Final = "acquit/selection-v1"
WITNESSES_SCHEMA: Final = "acquit/witnesses-v1"

ENV_SELECTION_FILE: Final = "ACQUIT_SELECTION_FILE"

DEFAULT_REPORT_FILE: Final = "acquit-report.json"
DEFAULT_SELECTION_FILE: Final = "acquit-selection.json"
DEFAULT_WITNESSES_FILE: Final = "acquit-witnesses.json"

COMMENT_MARKER: Final = "<!-- acquit-report -->"

CACHE_DIR: Final = ".acquit/cache"
PARSE_CACHE_DIR: Final = CACHE_DIR + "/parse"
