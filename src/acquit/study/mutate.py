"""Deterministic first-order mutants for the study's mutation-injection arm.

Merged PRs almost always keep the suite green, so outcome diffing alone has
near-zero power against a subtly wrong skip. An injected fault in a changed
file is a defect with a known location: the full suite and the acquit-selected
set both get a chance to catch it, and a fault only the full suite catches is
direct evidence of an unsound selection. Enumeration is deterministic on
purpose: no randomness, mutants ordered by source position and kind, capped
per file by even spacing, so re-running the same study sees the same mutants.
"""

import ast
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class MutantKind(StrEnum):
    """Enum order is the tiebreak between kinds at the same source position."""

    COMPARE_FLIP = "compare-flip"
    ARITH_FLIP = "arith-flip"
    BOUNDARY = "boundary"
    BOOL_FLIP = "bool-flip"
    RETURN_NEGATE = "return-negate"
    STRING_TWEAK = "string-tweak"


class MutantTarget(StrEnum):
    """What part of a module a mutant may land in.

    FUNCTION_BODIES_AND_CONSTANTS is ADR 0008's protocol for import-time-only
    files: mutate only where the change cannot alter module-level control
    flow, meaning statements inside def and class bodies plus module-level
    constant assignments. Everything else at module scope is off limits.
    """

    ALL = "all"
    FUNCTION_BODIES_AND_CONSTANTS = "function-bodies-and-constants"


@dataclass(frozen=True, slots=True)
class Mutant:
    """One mutated copy of a source file; line and col index the original."""

    source: str
    line: int
    col: int
    kind: MutantKind
    description: str


DEFAULT_CAP: Final = 10

_COMPARE_FLIPS: Final[dict[type[ast.cmpop], type[ast.cmpop]]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
}

_ARITH_FLIPS: Final[dict[type[ast.operator], type[ast.operator]]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
}

_OP_TEXT: Final[dict[type[ast.AST], str]] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Add: "+",
    ast.Sub: "-",
}

_KIND_ORDER: Final[dict[MutantKind, int]] = {kind: index for index, kind in enumerate(MutantKind)}


@dataclass(frozen=True, slots=True)
class _Candidate:
    line: int
    col: int
    kind: MutantKind
    description: str
    apply: Callable[[], None]


def detection_parity(kills: Iterable[tuple[bool, bool]]) -> float:
    """killed_by_selected over killed_by_full, on mutants the full suite killed.

    Each pair is (killed_by_selected, killed_by_full). When the full suite
    killed nothing there was nothing to miss, so parity is 1.0 by definition.
    """
    caught = 0
    full_kills = 0
    for killed_by_selected, killed_by_full in kills:
        if killed_by_full:
            full_kills += 1
            if killed_by_selected:
                caught += 1
    if full_kills == 0:
        return 1.0
    return caught / full_kills


def _docstring_ids(tree: ast.Module) -> frozenset[int]:
    """Identity set of docstring constants; mutating them proves nothing."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr):
            head = body[0].value
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                ids.add(id(head))
    return frozenset(ids)


def _swap_compare(node: ast.Compare, index: int, flipped: type[ast.cmpop]) -> Callable[[], None]:
    def apply() -> None:
        node.ops[index] = flipped()

    return apply


def _swap_binop(node: ast.BinOp, flipped: type[ast.operator]) -> Callable[[], None]:
    def apply() -> None:
        node.op = flipped()

    return apply


def _set_constant(node: ast.Constant, value: str | int) -> Callable[[], None]:
    def apply() -> None:
        node.value = value

    return apply


def _negate_return(node: ast.Return) -> Callable[[], None]:
    def apply() -> None:
        if node.value is not None:
            node.value = ast.UnaryOp(op=ast.Not(), operand=node.value)

    return apply


def _boolean_shaped(value: ast.expr | None) -> bool:
    if isinstance(value, ast.Compare | ast.BoolOp):
        return True
    return isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.Not)


def _constant_candidates(node: ast.Constant, docstrings: frozenset[int]) -> list[_Candidate]:
    if id(node) in docstrings:
        return []
    value = node.value
    if value is True or value is False:
        return [
            _Candidate(
                line=node.lineno,
                col=node.col_offset,
                kind=MutantKind.BOOL_FLIP,
                description=f"{value} to {not value}",
                apply=_set_constant(node, not value),
            )
        ]
    # bool is an int subclass; the boundary tweak wants genuine integers only
    if type(value) is int:
        return [
            _Candidate(
                line=node.lineno,
                col=node.col_offset,
                kind=MutantKind.BOUNDARY,
                description=f"{value} to {value + 1}",
                apply=_set_constant(node, value + 1),
            )
        ]
    if isinstance(value, str):
        return [
            _Candidate(
                line=node.lineno,
                col=node.col_offset,
                kind=MutantKind.STRING_TWEAK,
                description="string constant extended",
                apply=_set_constant(node, value + "x"),
            )
        ]
    return []


def _candidates_of(node: ast.AST, docstrings: frozenset[int]) -> list[_Candidate]:
    if isinstance(node, ast.Compare):
        found: list[_Candidate] = []
        for index, op in enumerate(node.ops):
            flipped_cmp = _COMPARE_FLIPS.get(type(op))
            if flipped_cmp is not None:
                found.append(
                    _Candidate(
                        line=node.lineno,
                        col=node.col_offset,
                        kind=MutantKind.COMPARE_FLIP,
                        description=f"{_OP_TEXT[type(op)]} to {_OP_TEXT[flipped_cmp]}",
                        apply=_swap_compare(node, index, flipped_cmp),
                    )
                )
        return found
    if isinstance(node, ast.BinOp):
        flipped_op = _ARITH_FLIPS.get(type(node.op))
        if flipped_op is None:
            return []
        return [
            _Candidate(
                line=node.lineno,
                col=node.col_offset,
                kind=MutantKind.ARITH_FLIP,
                description=f"{_OP_TEXT[type(node.op)]} to {_OP_TEXT[flipped_op]}",
                apply=_swap_binop(node, flipped_op),
            )
        ]
    if isinstance(node, ast.Return) and _boolean_shaped(node.value):
        return [
            _Candidate(
                line=node.lineno,
                col=node.col_offset,
                kind=MutantKind.RETURN_NEGATE,
                description="return value negated",
                apply=_negate_return(node),
            )
        ]
    if isinstance(node, ast.Constant):
        return _constant_candidates(node, docstrings)
    return []


def _collect(tree: ast.Module, target: MutantTarget) -> list[_Candidate]:
    """Walk the tree in source order, gathering every eligible mutation site."""
    docstrings = _docstring_ids(tree)
    restricted = target is MutantTarget.FUNCTION_BODIES_AND_CONSTANTS
    collected: list[_Candidate] = []

    def visit(node: ast.AST, allowed: bool) -> None:
        if allowed:
            collected.extend(_candidates_of(node, docstrings))
        if isinstance(node, ast.Module):
            for statement in node.body:
                # only a top-level Assign or AnnAssign is a module constant
                constant = isinstance(statement, ast.Assign | ast.AnnAssign)
                visit(statement, not restricted or constant)
            return
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            in_body = {id(child) for child in node.body}
            for child in ast.iter_child_nodes(node):
                # decorators, defaults, and bases run at definition time,
                # so they keep the surrounding eligibility, not the body's
                visit(child, id(child) in in_body or allowed)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, allowed)

    visit(tree, not restricted)
    # stable sort: same-position candidates keep source order within a kind
    collected.sort(key=lambda entry: (entry.line, entry.col, _KIND_ORDER[entry.kind]))
    return collected


def _spaced(total: int, cap: int) -> list[int]:
    """Evenly spaced indices keep a capped sample representative of the file."""
    if total <= 0 or cap <= 0:
        return []
    if total <= cap:
        return list(range(total))
    if cap == 1:
        return [0]
    return [(index * (total - 1)) // (cap - 1) for index in range(cap)]


def enumerate_mutants(
    source: str,
    target: MutantTarget = MutantTarget.ALL,
    cap: int = DEFAULT_CAP,
) -> list[Mutant]:
    """Enumerate first-order mutants of one Python source, deterministically.

    Source that does not parse contributes no mutants instead of failing the
    arm. Each mutant is produced from a fresh parse with exactly one site
    changed and the whole tree unparsed, so mutants are independent.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    try:
        total = len(_collect(tree, target))
        mutants: list[Mutant] = []
        for index in _spaced(total, cap):
            fresh = ast.parse(source)
            candidate = _collect(fresh, target)[index]
            candidate.apply()
            mutants.append(
                Mutant(
                    source=ast.unparse(fresh),
                    line=candidate.line,
                    col=candidate.col,
                    kind=candidate.kind,
                    description=candidate.description,
                )
            )
        return mutants
    except RecursionError:
        # pathological nesting; treated the same as source that will not parse
        return []
