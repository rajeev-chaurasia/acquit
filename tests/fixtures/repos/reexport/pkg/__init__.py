"""Package pkg: a pure re-exporter over its two submodules."""

from .console import Console
from .table import Table

__all__ = ["Console", "Table"]
__version__ = "0.1.0"
