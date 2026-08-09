"""Watcher: copies the quiet constant at import time, so it is not inert."""

from .quiet import LIMIT

SEEN = list(range(LIMIT))
