"""Console rendering: constructs state at import time, so it is not inert."""

_THEMES = dict(plain="", fancy="*")


class Console:
    def banner(self) -> str:
        return "console" + _THEMES["fancy"]
