"""Junit XML parsing and node-id normalization for the replay study.

Acquit selects whole test files, so outcomes roll up twice: parametrized
cases collapse to a normalized function id (file path and class chain kept,
parameter brackets stripped), and function ids roll up to files. A function
that runs under several parameters carries the set of outcomes it produced.
"""

import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from acquit.errors import AcquitError


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class SuiteOutcomes:
    """One suite run: outcome sets per normalized test id, seconds per file."""

    by_test: Mapping[str, frozenset[Outcome]]
    file_durations: Mapping[str, float]


def _split_classname(classname: str) -> tuple[str, tuple[str, ...]]:
    """Split a junit classname into a module file path and the class chain.

    pytest writes the dotted module path followed by any class names. Class
    names start with an uppercase letter and module components do not, which
    is a heuristic, but it is the only signal xunit2 output leaves behind.
    """
    module: list[str] = []
    classes: list[str] = []
    for part in classname.split("."):
        if classes or part[:1].isupper():
            classes.append(part)
        else:
            module.append(part)
    if not module:
        # A bare class chain names no module; keep it deterministic anyway.
        return classname.replace(".", "/") + ".py", ()
    return "/".join(module) + ".py", tuple(classes)


def normalize_node_id(classname: str, name: str, file: str | None = None) -> str:
    """Collapse one junit testcase to file::Class::function form.

    Parametrize ids are stripped, the class chain is kept. When the xml
    carries a file attribute (the xunit1 family) it wins over the classname
    heuristic, because it is the real repo-relative path.
    """
    bare = name.partition("[")[0]
    path, classes = _split_classname(classname)
    if file:
        path = file.replace("\\", "/")
    return "::".join([path, *classes, bare])


def file_of(node_id: str) -> str:
    return node_id.partition("::")[0]


def _outcome_of(case: ET.Element) -> Outcome:
    tags = {child.tag for child in case}
    if "error" in tags:
        return Outcome.ERROR
    if "failure" in tags:
        return Outcome.FAILED
    if "skipped" in tags:
        return Outcome.SKIPPED
    return Outcome.PASSED


def _seconds(raw: str | None) -> float:
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


JUNIT_SIZE_CAP: Final = 100 * 1024 * 1024


def parse_junit(text: str) -> SuiteOutcomes:
    """Parse one junit xml document into normalized outcomes and durations."""
    # The document is produced by pytest, but pytest just executed the target
    # repo's code, so treat it as untrusted: cap the size before parsing.
    # Entity-expansion attacks are additionally blunted by modern expat's
    # built-in amplification limits.
    if len(text) > JUNIT_SIZE_CAP:
        raise AcquitError(f"junit xml exceeds {JUNIT_SIZE_CAP} bytes; refusing to parse")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise AcquitError(f"junit xml is not parseable: {error}") from error
    by_test: dict[str, set[Outcome]] = {}
    durations: dict[str, float] = {}
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        if not name:
            continue
        node = normalize_node_id(case.get("classname") or "", name, case.get("file"))
        by_test.setdefault(node, set()).add(_outcome_of(case))
        path = file_of(node)
        durations[path] = durations.get(path, 0.0) + _seconds(case.get("time"))
    return SuiteOutcomes(
        by_test={node: frozenset(values) for node, values in by_test.items()},
        file_durations=durations,
    )
