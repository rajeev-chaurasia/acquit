"""Package legacy: its init defines behavior, so it never qualifies."""

from .engine import Engine


def make_engine() -> Engine:
    return Engine()
