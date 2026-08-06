"""Conftest scoping and static hook inspection.

pytest applies every conftest.py on the directory chain from the repo root
down to a test's directory. The edges built here make that implicit coupling
explicit in the dependency graph.
"""

import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from acquit.graph.model import Edge, EdgeKind

UNPARSEABLE_MARKER: Final = "__unparseable__"

_COLLECTION_NAMES: Final = frozenset(
    {
        "pytest_collect_file",
        "pytest_ignore_collect",
        "pytest_pycollect_makemodule",
        "collect_ignore",
        "collect_ignore_glob",
    }
)


@dataclass(frozen=True, slots=True)
class ConftestFacts:
    """Statically extracted facts about one conftest.py."""

    path: str
    collection_altering: tuple[str, ...]
    pytest_plugins: tuple[str, ...]


def _dirname(path: str) -> str:
    head, _, _ = path.rpartition("/")
    return head


def _is_ancestor_dir(ancestor: str, descendant: str) -> bool:
    return ancestor == "" or descendant == ancestor or descendant.startswith(ancestor + "/")


def conftest_scope_edges(test_path: str, conftest_paths: Sequence[str]) -> tuple[Edge, ...]:
    """Edges from a test file to every conftest pytest would apply to it."""
    test_dir = _dirname(test_path)
    return tuple(
        Edge(src=test_path, dst=conftest, kind=EdgeKind.CONFTEST_SCOPE)
        for conftest in sorted(set(conftest_paths))
        if conftest.rsplit("/", 1)[-1] == "conftest.py"
        and _is_ancestor_dir(_dirname(conftest), test_dir)
    )


def _target_names(target: ast.expr) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            yield from _target_names(element)


def _defined_names(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        yield node.name
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            yield from _target_names(target)
    elif isinstance(node, ast.AnnAssign | ast.AugAssign) and isinstance(node.target, ast.Name):
        yield node.target.id


def _string_literals(value: ast.expr) -> Iterator[str]:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        yield value.value
    elif isinstance(value, ast.Tuple | ast.List):
        for element in value.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                yield element.value


def _pytest_plugins_value(statement: ast.stmt) -> ast.expr | None:
    if isinstance(statement, ast.Assign):
        for target in statement.targets:
            if "pytest_plugins" in _target_names(target):
                return statement.value
    if (
        isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id == "pytest_plugins"
    ):
        return statement.value
    return None


def inspect_conftest(source: bytes, path: str) -> ConftestFacts:
    """Extract collection-relevant facts from conftest source, without importing it.

    On any parse failure the facts carry an "__unparseable__" marker in
    collection_altering so callers fail closed instead of trusting silence.
    """
    try:
        module = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return ConftestFacts(
            path=path, collection_altering=(UNPARSEABLE_MARKER,), pytest_plugins=()
        )

    # Nested definitions count too: over-reporting is the safe direction.
    altering = {
        name
        for node in ast.walk(module)
        for name in _defined_names(node)
        if name in _COLLECTION_NAMES
    }

    plugins: list[str] = []
    for statement in module.body:
        value = _pytest_plugins_value(statement)
        if value is None:
            continue
        for literal in _string_literals(value):
            if literal not in plugins:
                plugins.append(literal)

    return ConftestFacts(
        path=path,
        collection_altering=tuple(sorted(altering)),
        pytest_plugins=tuple(plugins),
    )
