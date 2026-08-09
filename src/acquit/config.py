"""Acquit's own configuration.

Sources, first hit wins: .acquit.toml at the repo root (top-level keys), then
[tool.acquit] in pyproject.toml. Missing files mean defaults. Unknown keys are
rejected: a typo in a soundness-sensitive config must not be silently ignored.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acquit.errors import PolicyError

_KNOWN_KEYS = frozenset({"roots", "assume_inert", "narrowing", "waive"})
_WAIVER_KEYS = ("rule", "glob", "justification")


@dataclass(frozen=True, slots=True)
class Waiver:
    rule: str
    glob: str
    # Mandatory: a waiver nobody can justify is not reviewable.
    justification: str


@dataclass(frozen=True, slots=True)
class AcquitConfig:
    roots: tuple[str, ...] = ()
    # Globs of resource files the user vouches never affect tests.
    assume_inert: tuple[str, ...] = ()
    waivers: tuple[Waiver, ...] = ()
    # Re-export narrowing (ADR 0008). Ships disabled; the rollout is
    # evidence-gated, and working-tree selections never narrow either way.
    narrowing: bool = False


def load_config(repo_root: Path) -> AcquitConfig:
    """Load configuration from repo_root, falling back to defaults.

    Raises PolicyError on unreadable TOML, unknown keys, or malformed waivers.
    """
    acquit_toml = repo_root / ".acquit.toml"
    if acquit_toml.is_file():
        return _parse(_read_toml(acquit_toml), source=".acquit.toml")
    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        tool = _read_toml(pyproject).get("tool")
        section = tool.get("acquit") if isinstance(tool, dict) else None
        if section is not None:
            if not isinstance(section, dict):
                raise PolicyError("pyproject.toml: [tool.acquit] must be a table")
            return _parse(section, source="pyproject.toml [tool.acquit]")
    return AcquitConfig()


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as error:
        raise PolicyError(f"{path.name}: cannot read: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise PolicyError(f"{path.name}: invalid TOML: {error}") from error


def _parse(data: dict[str, Any], source: str) -> AcquitConfig:
    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise PolicyError(f"{source}: unknown key(s): {', '.join(unknown)}")
    return AcquitConfig(
        roots=_string_tuple(data.get("roots", []), key="roots", source=source),
        assume_inert=_string_tuple(data.get("assume_inert", []), key="assume_inert", source=source),
        waivers=_parse_waivers(data.get("waive", []), source=source),
        narrowing=_flag(data.get("narrowing", False), key="narrowing", source=source),
    )


def _string_tuple(value: Any, key: str, source: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyError(f"{source}: {key!r} must be an array of strings")
    return tuple(value)


def _flag(value: Any, key: str, source: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyError(f"{source}: {key!r} must be a boolean")
    return value


def _parse_waivers(value: Any, source: str) -> tuple[Waiver, ...]:
    if not isinstance(value, list):
        raise PolicyError(f"{source}: 'waive' must be an array of tables")
    waivers: list[Waiver] = []
    for position, entry in enumerate(value, start=1):
        label = f"{source}: waive entry {position}"
        if not isinstance(entry, dict):
            raise PolicyError(f"{label} must be a table")
        if isinstance(entry.get("rule"), str):
            label = f"{label} (rule {entry['rule']})"
        unknown = sorted(set(entry) - set(_WAIVER_KEYS))
        if unknown:
            raise PolicyError(f"{label}: unknown key(s): {', '.join(unknown)}")
        missing = [key for key in _WAIVER_KEYS if key not in entry]
        if missing:
            raise PolicyError(f"{label}: missing key(s): {', '.join(missing)}")
        for key in _WAIVER_KEYS:
            if not isinstance(entry[key], str):
                raise PolicyError(f"{label}: {key!r} must be a string")
        if not entry["justification"].strip():
            raise PolicyError(f"{label}: justification must not be empty")
        waivers.append(
            Waiver(rule=entry["rule"], glob=entry["glob"], justification=entry["justification"])
        )
    return tuple(waivers)
