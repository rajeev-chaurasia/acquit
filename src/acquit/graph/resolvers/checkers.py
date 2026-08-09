"""Pure AST checkers behind re-export narrowing (ADR 0008).

Three fail-closed whitelists over one parsed module: the pure re-exporter
scan for package inits, the import-inertness check for submodules, and the
module-level bound-name set that the relational witness conditions compare
across revisions. Everything here is a deterministic function of one
module's AST; nothing touches the index, the graph, or the filesystem.
"""

import ast
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# Module attribute hooks: binding any of these makes runtime attribute access
# or path resolution on the module run code a body-only diff could change.
_MODULE_HOOKS: Final = frozenset({"__getattr__", "__dir__", "__path__"})
_HOOK_DEFS: Final = frozenset({"__getattr__", "__dir__"})
# A local class defining either hook runs code when subclassed or bound.
_CLASS_HOOK_DEFS: Final = frozenset({"__init_subclass__", "__set_name__"})
_ALL: Final = "__all__"
_TYPE_CHECKING: Final = "TYPE_CHECKING"
_TYPING: Final = "typing"
_FUTURE: Final = "__future__"


@dataclass(frozen=True, slots=True)
class Verdict:
    """One checker outcome; reason names the first disqualifier, "" on pass."""

    ok: bool
    reason: str = ""


_OK: Final = Verdict(ok=True)


class ReexportTier(StrEnum):
    """How an init proved pure: the plain whitelist, or star over a literal __all__."""

    STRICT = "strict"
    STAR_ALL = "star-over-literal-all"


class BindingForm(StrEnum):
    """How an init's import statement binds a name at runtime."""

    # from X import name [as alias]: binds a member of the source module.
    FROM_IMPORT = "from-import"
    # import X [as alias]: binds the module object itself.
    MODULE_IMPORT = "module-import"


@dataclass(frozen=True, slots=True)
class ReexportBinding:
    """One runtime name an init binds through an import statement.

    For FROM_IMPORT, module/level locate the source ("" for pure-relative)
    and member is the original imported name. For MODULE_IMPORT, module is
    the absolute dotted module the name gives access to; a bare `import a.b`
    yields one binding per dotted prefix because the import machinery wires
    each submodule onto its parent as an attribute.
    """

    name: str
    form: BindingForm
    module: str
    level: int
    member: str


@dataclass(frozen=True, slots=True)
class StarImport:
    """One star re-export awaiting tier-two verification of its source."""

    module: str
    level: int


@dataclass(frozen=True, slots=True)
class ReexportScan:
    """Content-based re-export facts for one module.

    reason is None when every statement passes the pure re-exporter
    statement whitelist (star sources still pending verification), else it
    names the first disqualifier. all_names carries the module's single
    literal __all__ so it can serve as a star source; it is computed for
    every module, whitelisted or not.
    """

    reason: str | None
    bindings: tuple[ReexportBinding, ...]
    stars: tuple[StarImport, ...]
    local_names: tuple[str, ...]
    all_names: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class InitVerdict:
    """Pure re-exporter outcome: the tier proven, or the disqualifier."""

    tier: ReexportTier | None
    reason: str = ""


# Resolves a star source (module, level) to its literal __all__ names, or
# None when the source cannot be pinned to one file with one literal __all__.
StarSourceAllNames = Callable[[str, int], "tuple[str, ...] | None"]


def pure_reexporter(
    tree: ast.Module, star_all_names: StarSourceAllNames | None = None
) -> InitVerdict:
    """Prove one init pure per the ADR 0008 whitelist, or name the disqualifier.

    star_all_names resolves a star source to its literal __all__; without it
    every star import disqualifies (the strict tier).
    """
    return reexporter_verdict(scan_reexports(tree), star_all_names)


def reexporter_verdict(
    scan: ReexportScan, star_all_names: StarSourceAllNames | None
) -> InitVerdict:
    """The tier a scanned init proves, once its star sources are resolvable."""
    if scan.reason is not None:
        return InitVerdict(tier=None, reason=scan.reason)
    if not scan.stars:
        return InitVerdict(tier=ReexportTier.STRICT)
    if star_all_names is None:
        return InitVerdict(tier=None, reason="star-import")
    for star in scan.stars:
        if star_all_names(star.module, star.level) is None:
            return InitVerdict(tier=None, reason="star-source-not-literal-all")
    return InitVerdict(tier=ReexportTier.STAR_ALL)


def scan_reexports(tree: ast.Module) -> ReexportScan:
    """Scan one module against the pure re-exporter statement whitelist."""
    ctx = _GuardContext.from_tree(tree)
    future_annotations = _has_future_annotations(tree)
    bindings: list[ReexportBinding] = []
    stars: list[StarImport] = []
    local_names: list[str] = []
    reason: str | None = None
    for stmt in tree.body:
        verdict = _init_stmt(stmt, ctx, future_annotations, bindings, stars, local_names)
        if not verdict.ok:
            reason = verdict.reason
            break
    return ReexportScan(
        reason=reason,
        bindings=tuple(bindings) if reason is None else (),
        stars=tuple(stars) if reason is None else (),
        local_names=tuple(dict.fromkeys(local_names)) if reason is None else (),
        all_names=_single_literal_all(tree),
    )


def module_inertness(tree: ast.Module) -> Verdict:
    """Decide whether importing this module can only bind names in its namespace.

    The strict whitelist from ADR 0008: every admitted statement binds names
    without executing user code, and anything debatable rejects. "Binds
    names" includes triggering the module's imports, which is why the
    relational witness conditions pin the bound-name and edge sets across
    revisions on top of this per-revision check. A rejection is always
    sound; it keeps the file a full-impact member of every closure.
    """
    return _InertChecker(tree).check(tree)


def bound_name_set(tree: ast.Module) -> frozenset[str]:
    """Every name the module's import-time execution can bind at module level.

    The relational witness condition compares this set across revisions, so
    the collection mirrors the statements the inertness whitelist admits:
    TYPE_CHECKING-guarded bodies never run and contribute nothing, while
    every block of an admitted try/except contributes (whichever branch
    runs, the union bounds what a revision may bind). Statements outside
    the whitelist are collected best-effort; the check is only ever applied
    to whitelist-inert files.
    """
    ctx = _GuardContext.from_tree(tree)
    names: set[str] = set()
    _collect_bound(tree.body, ctx, names)
    return frozenset(names)


# ---------------------------------------------------------------------------
# TYPE_CHECKING guard recognition, shared by every checker
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _GuardContext:
    """Names provably usable as a TYPE_CHECKING guard, shadow-checked."""

    typing_modules: frozenset[str]
    type_checking_names: frozenset[str]
    bare_ok: bool

    @classmethod
    def from_tree(cls, tree: ast.Module) -> "_GuardContext":
        module_counts: dict[str, int] = {}
        tc_counts: dict[str, int] = {}
        rebound: set[str] = set()
        for stmt in tree.body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    bound = alias.asname or alias.name.partition(".")[0]
                    if alias.name == _TYPING:
                        module_counts[bound] = module_counts.get(bound, 0) + 1
                    else:
                        rebound.add(bound)
            elif isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    bound = alias.asname or alias.name
                    if stmt.module == _TYPING and alias.name == _TYPE_CHECKING:
                        tc_counts[bound] = tc_counts.get(bound, 0) + 1
                    else:
                        rebound.add(bound)
            else:
                rebound.update(_stored_names(stmt))
        modules = frozenset(
            name for name, count in module_counts.items() if count == 1 and name not in rebound
        )
        tc_names = frozenset(
            name for name, count in tc_counts.items() if count == 1 and name not in rebound
        )
        return cls(
            typing_modules=modules,
            type_checking_names=tc_names,
            bare_ok=_TYPE_CHECKING not in rebound,
        )


def _stored_names(stmt: ast.stmt) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(stmt):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return names


def _is_type_checking_guard(test: ast.expr, ctx: _GuardContext) -> bool:
    # A bare TYPE_CHECKING is trusted only when nothing unqualified rebinds
    # it; anything debatable falls through and rejects as a conditional.
    if isinstance(test, ast.Name):
        return test.id in ctx.type_checking_names or (test.id == _TYPE_CHECKING and ctx.bare_ok)
    if isinstance(test, ast.Attribute) and test.attr == _TYPE_CHECKING:
        return isinstance(test.value, ast.Name) and test.value.id in ctx.typing_modules
    return False


def _has_future_annotations(tree: ast.Module) -> bool:
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.ImportFrom)
            and stmt.module == _FUTURE
            and any(alias.name == "annotations" for alias in stmt.names)
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Shared expression grammar
# ---------------------------------------------------------------------------


def _is_const_expr(node: ast.expr) -> bool:
    """Literals and operator trees over literals; operators on names reject."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp):
        return _is_const_expr(node.operand)
    if isinstance(node, ast.BinOp):
        return _is_const_expr(node.left) and _is_const_expr(node.right)
    return False


def _is_const_display(node: ast.expr) -> bool:
    """The class-body value regime: constants and displays of constants only.

    A bare name is inert at module level but not here: binding an object
    into a class namespace invokes __set_name__ on the value's type.
    """
    if _is_const_expr(node):
        return True
    if isinstance(node, ast.Tuple | ast.List):
        return all(_is_const_display(element) for element in node.elts)
    if isinstance(node, ast.Set):
        # Set elements are hashed at construction: constants only.
        return all(_is_const_expr(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        keys_ok = all(key is not None and _is_const_expr(key) for key in node.keys)
        return keys_ok and all(_is_const_display(value) for value in node.values)
    return False


def _is_inert_expr(node: ast.expr) -> bool:
    """The module-level value regime: provably free of user-code execution.

    Plain name loads run no user code (a module __getattr__ fires on
    attribute access on module objects, not on loads inside the module);
    attribute access, subscripts, calls, and f-strings all reject.
    """
    if _is_const_expr(node):
        return True
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        return True
    if isinstance(node, ast.Tuple | ast.List):
        return all(_is_inert_expr(element) for element in node.elts)
    if isinstance(node, ast.Set):
        return all(_is_const_expr(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        keys_ok = all(key is not None and _is_const_expr(key) for key in node.keys)
        return keys_ok and all(_is_inert_expr(value) for value in node.values)
    if isinstance(node, ast.Lambda):
        # The body never runs at import; only the defaults evaluate.
        return _defaults_inert(node.args)
    return False


def _defaults_inert(args: ast.arguments) -> bool:
    defaults = [*args.defaults, *(d for d in args.kw_defaults if d is not None)]
    return all(_is_inert_expr(default) for default in defaults)


def _annotations_of(stmt: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    args = stmt.args
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        every.append(args.vararg)
    if args.kwarg is not None:
        every.append(args.kwarg)
    collected = [arg.annotation for arg in every if arg.annotation is not None]
    if stmt.returns is not None:
        collected.append(stmt.returns)
    return collected


def _is_literal_str_sequence(node: ast.expr) -> bool:
    return isinstance(node, ast.List | ast.Tuple) and all(
        isinstance(element, ast.Constant) and isinstance(element.value, str)
        for element in node.elts
    )


def _stmt_name(stmt: ast.stmt) -> str:
    return type(stmt).__name__.lower()


# ---------------------------------------------------------------------------
# The pure re-exporter statement whitelist
# ---------------------------------------------------------------------------


def _init_stmt(
    stmt: ast.stmt,
    ctx: _GuardContext,
    future_annotations: bool,
    bindings: list[ReexportBinding],
    stars: list[StarImport],
    local_names: list[str],
) -> Verdict:
    if isinstance(stmt, ast.Expr):
        if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            return _OK  # docstring or stray string literal, a no-op
        return Verdict(ok=False, reason="expression")
    if isinstance(stmt, ast.ImportFrom):
        return _init_import_from(stmt, bindings, stars, local_names)
    if isinstance(stmt, ast.Import):
        _collect_module_imports(stmt, bindings)
        return _OK
    if isinstance(stmt, ast.Assign):
        return _init_assign(stmt, local_names)
    if isinstance(stmt, ast.AnnAssign):
        return _init_ann_assign(stmt, future_annotations, local_names)
    if isinstance(stmt, ast.If):
        if _is_type_checking_guard(stmt.test, ctx) and not stmt.orelse:
            return _OK  # the guarded body never executes at runtime
        return Verdict(ok=False, reason="conditional")
    if isinstance(stmt, ast.Try):
        # try/except ImportError makes the bound-name set environment-dependent.
        return Verdict(ok=False, reason="conditional-import")
    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
        return Verdict(ok=False, reason="def")
    if isinstance(stmt, ast.ClassDef):
        return Verdict(ok=False, reason="class")
    if isinstance(stmt, ast.Pass):
        return _OK
    return Verdict(ok=False, reason=_stmt_name(stmt))


def _init_import_from(
    stmt: ast.ImportFrom,
    bindings: list[ReexportBinding],
    stars: list[StarImport],
    local_names: list[str],
) -> Verdict:
    module = stmt.module if stmt.module is not None else ""
    if stmt.level == 0 and module == _FUTURE:
        # A future import binds a feature object; its home is the init itself.
        local_names.extend(alias.asname or alias.name for alias in stmt.names)
        return _OK
    if any(alias.name == "*" for alias in stmt.names):
        stars.append(StarImport(module=module, level=stmt.level))
        return _OK
    for alias in stmt.names:
        bindings.append(
            ReexportBinding(
                name=alias.asname or alias.name,
                form=BindingForm.FROM_IMPORT,
                module=module,
                level=stmt.level,
                member=alias.name,
            )
        )
    return _OK


def _collect_module_imports(stmt: ast.Import, bindings: list[ReexportBinding]) -> None:
    for alias in stmt.names:
        if alias.asname is not None:
            bindings.append(
                ReexportBinding(
                    name=alias.asname,
                    form=BindingForm.MODULE_IMPORT,
                    module=alias.name,
                    level=0,
                    member="",
                )
            )
            continue
        # import a.b binds a, and the import machinery wires each submodule
        # onto its parent, so the name reaches every dotted prefix.
        top = alias.name.partition(".")[0]
        prefix = ""
        for part in alias.name.split("."):
            prefix = f"{prefix}.{part}" if prefix else part
            bindings.append(
                ReexportBinding(
                    name=top,
                    form=BindingForm.MODULE_IMPORT,
                    module=prefix,
                    level=0,
                    member="",
                )
            )


def _init_assign(stmt: ast.Assign, local_names: list[str]) -> Verdict:
    names: list[str] = []
    for target in stmt.targets:
        if not isinstance(target, ast.Name):
            return Verdict(ok=False, reason="non-name-target")
        if target.id in _MODULE_HOOKS:
            return Verdict(ok=False, reason="module-hook-assignment")
        names.append(target.id)
    if _ALL in names:
        if not _is_literal_str_sequence(stmt.value):
            return Verdict(ok=False, reason="non-literal-__all__")
    elif not isinstance(stmt.value, ast.Constant):
        return Verdict(ok=False, reason="non-literal-assignment")
    local_names.extend(names)
    return _OK


def _init_ann_assign(
    stmt: ast.AnnAssign, future_annotations: bool, local_names: list[str]
) -> Verdict:
    if not isinstance(stmt.target, ast.Name):
        return Verdict(ok=False, reason="non-name-target")
    if stmt.target.id in _MODULE_HOOKS:
        return Verdict(ok=False, reason="module-hook-assignment")
    if not future_annotations and not _is_inert_expr(stmt.annotation):
        # Module-level annotations evaluate eagerly without the future import.
        return Verdict(ok=False, reason="evaluated-annotation")
    if stmt.target.id == _ALL:
        if stmt.value is None or not _is_literal_str_sequence(stmt.value):
            return Verdict(ok=False, reason="non-literal-__all__")
    elif stmt.value is not None and not isinstance(stmt.value, ast.Constant):
        return Verdict(ok=False, reason="non-literal-assignment")
    if stmt.value is not None:
        local_names.append(stmt.target.id)
    return _OK


def _single_literal_all(tree: ast.Module) -> tuple[str, ...] | None:
    """The module's __all__ names when assigned exactly once as string literals.

    Any other module-level mention of __all__ (augmentation, a computed
    rebind, a conditional) makes the star-bound set dynamic, so None.
    """
    found: tuple[str, ...] | None = None
    count = 0
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == _ALL
            and _is_literal_str_sequence(stmt.value)
            and isinstance(stmt.value, ast.List | ast.Tuple)
        ):
            count += 1
            found = tuple(
                element.value
                for element in stmt.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and node.id == _ALL:
                return None
    return found if count == 1 else None


# ---------------------------------------------------------------------------
# The inertness whitelist
# ---------------------------------------------------------------------------


class _InertChecker:
    def __init__(self, tree: ast.Module) -> None:
        self._future_annotations = _has_future_annotations(tree)
        self._guard = _GuardContext.from_tree(tree)
        # Local classes proven hookless, admissible as bases further down.
        # Rebinding the name evicts it: the base must still be that class.
        self._hookless: set[str] = set()

    def check(self, tree: ast.Module) -> Verdict:
        return self._block(tree.body)

    def _block(self, stmts: list[ast.stmt]) -> Verdict:
        for stmt in stmts:
            verdict = self._module_stmt(stmt)
            if not verdict.ok:
                return verdict
        return _OK

    def _module_stmt(self, stmt: ast.stmt) -> Verdict:
        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant):
                return _OK
            return Verdict(ok=False, reason="expression")
        if isinstance(stmt, ast.ImportFrom):
            if any(alias.name == "*" for alias in stmt.names):
                # Star evaluates __all__ on the source, or walks its dict.
                return Verdict(ok=False, reason="star-import")
            self._hookless.difference_update(alias.asname or alias.name for alias in stmt.names)
            return _OK
        if isinstance(stmt, ast.Import):
            self._hookless.difference_update(
                alias.asname or alias.name.partition(".")[0] for alias in stmt.names
            )
            return _OK
        if isinstance(stmt, ast.Assign):
            return self._assign(stmt)
        if isinstance(stmt, ast.AnnAssign):
            return self._ann_assign(stmt)
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            self._hookless.discard(stmt.name)
            return self._def(stmt, in_class=False)
        if isinstance(stmt, ast.ClassDef):
            return self._class(stmt, register=True)
        if isinstance(stmt, ast.If):
            if _is_type_checking_guard(stmt.test, self._guard):
                # The guarded body never runs; the else branch is what runs.
                return self._block(stmt.orelse)
            return Verdict(ok=False, reason="conditional")
        if isinstance(stmt, ast.Try):
            return self._try(stmt)
        if isinstance(stmt, ast.Pass):
            return _OK
        return Verdict(ok=False, reason=_stmt_name(stmt))

    def _assign(self, stmt: ast.Assign) -> Verdict:
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                if target.id in _MODULE_HOOKS:
                    return Verdict(ok=False, reason="module-hook-assignment")
                self._hookless.discard(target.id)
                continue
            if isinstance(target, ast.Tuple | ast.List):
                for element in target.elts:
                    if not isinstance(element, ast.Name):
                        return Verdict(ok=False, reason="complex-unpack-target")
                    if element.id in _MODULE_HOOKS:
                        return Verdict(ok=False, reason="module-hook-assignment")
                    self._hookless.discard(element.id)
                # Unpacking anything but a literal display invokes user iteration.
                if not isinstance(stmt.value, ast.Tuple | ast.List):
                    return Verdict(ok=False, reason="unpack-from-non-display")
                continue
            return Verdict(ok=False, reason="non-name-target")
        if not _is_inert_expr(stmt.value):
            return Verdict(ok=False, reason="non-inert-value")
        return _OK

    def _ann_assign(self, stmt: ast.AnnAssign) -> Verdict:
        if not isinstance(stmt.target, ast.Name):
            return Verdict(ok=False, reason="non-name-target")
        if stmt.target.id in _MODULE_HOOKS:
            return Verdict(ok=False, reason="module-hook-assignment")
        self._hookless.discard(stmt.target.id)
        if not self._future_annotations and not _is_inert_expr(stmt.annotation):
            return Verdict(ok=False, reason="evaluated-annotation")
        if stmt.value is not None and not _is_inert_expr(stmt.value):
            return Verdict(ok=False, reason="non-inert-value")
        return _OK

    def _def(self, stmt: ast.FunctionDef | ast.AsyncFunctionDef, *, in_class: bool) -> Verdict:
        if not in_class and stmt.name in _HOOK_DEFS:
            return Verdict(ok=False, reason="module-hook-def")
        if stmt.decorator_list:
            return Verdict(ok=False, reason="decorator")
        if stmt.type_params:
            # PEP 695 type parameters construct TypeVars at definition time.
            return Verdict(ok=False, reason="type-params")
        if not _defaults_inert(stmt.args):
            # Default parameter values evaluate at import time.
            return Verdict(ok=False, reason="evaluated-default")
        if not self._future_annotations:
            for annotation in _annotations_of(stmt):
                if not _is_inert_expr(annotation):
                    return Verdict(ok=False, reason="evaluated-annotation")
        return _OK  # the body never executes at import time

    def _class(self, stmt: ast.ClassDef, *, register: bool) -> Verdict:
        if stmt.decorator_list:
            # A decorator is a call, dataclass and attrs included.
            return Verdict(ok=False, reason="class-decorator")
        if stmt.keywords:
            # metaclass= is arbitrary code; other keywords feed __init_subclass__.
            return Verdict(ok=False, reason="class-keywords")
        if stmt.type_params:
            return Verdict(ok=False, reason="type-params")
        for base in stmt.bases:
            # Imported bases reject categorically: a subclass-registry hook on
            # the base runs code at class creation (the design doc's sharpest
            # counterexample), and only local hookless classes are provable.
            if not (isinstance(base, ast.Name) and base.id in self._hookless):
                return Verdict(ok=False, reason="nonlocal-base")
        defines_hook = False
        for inner in stmt.body:
            verdict = self._class_body_stmt(inner)
            if not verdict.ok:
                return verdict
            if (
                isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef)
                and inner.name in _CLASS_HOOK_DEFS
            ):
                defines_hook = True
        if register and not defines_hook:
            self._hookless.add(stmt.name)
        return _OK

    def _class_body_stmt(self, stmt: ast.stmt) -> Verdict:
        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant):
                return _OK
            return Verdict(ok=False, reason="class-body-expression")
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            return self._def(stmt, in_class=True)
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if not isinstance(target, ast.Name):
                    return Verdict(ok=False, reason="non-name-target")
            if not _is_const_display(stmt.value):
                return Verdict(ok=False, reason="class-body-value")
            return _OK
        if isinstance(stmt, ast.AnnAssign):
            if not isinstance(stmt.target, ast.Name):
                return Verdict(ok=False, reason="non-name-target")
            if not self._future_annotations and not _is_inert_expr(stmt.annotation):
                return Verdict(ok=False, reason="evaluated-annotation")
            if stmt.value is not None and not _is_const_display(stmt.value):
                return Verdict(ok=False, reason="class-body-value")
            return _OK
        if isinstance(stmt, ast.ClassDef):
            # Nested classes follow the same rules but never become bases.
            return self._class(stmt, register=False)
        if isinstance(stmt, ast.If) and _is_type_checking_guard(stmt.test, self._guard):
            for inner in stmt.orelse:
                verdict = self._class_body_stmt(inner)
                if not verdict.ok:
                    return verdict
            return _OK
        if isinstance(stmt, ast.Pass):
            return _OK
        return Verdict(ok=False, reason="class-body-" + _stmt_name(stmt))

    def _try(self, stmt: ast.Try) -> Verdict:
        for handler in stmt.handlers:
            if handler.type is not None and not _is_exception_names(handler.type):
                return Verdict(ok=False, reason="computed-except-type")
        blocks = [stmt.body, *(handler.body for handler in stmt.handlers)]
        blocks.extend((stmt.orelse, stmt.finalbody))
        for block in blocks:
            verdict = self._block(block)
            if not verdict.ok:
                return verdict
        return _OK


def _is_exception_names(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Tuple):
        return all(isinstance(element, ast.Name) for element in node.elts)
    return False


# ---------------------------------------------------------------------------
# Module-level bound names, for the relational witness condition
# ---------------------------------------------------------------------------


def _collect_bound(stmts: list[ast.stmt], ctx: _GuardContext, names: set[str]) -> None:
    for stmt in stmts:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                names.add(alias.asname or alias.name.partition(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in stmt.names if alias.name != "*")
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                names.update(_target_names(target))
        elif isinstance(stmt, ast.AnnAssign):
            # An annotation without a value binds nothing at runtime.
            if stmt.value is not None and isinstance(stmt.target, ast.Name):
                names.add(stmt.target.id)
        elif isinstance(stmt, ast.AugAssign):
            names.update(_target_names(stmt.target))
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(stmt.name)
        elif isinstance(stmt, ast.If):
            if not _is_type_checking_guard(stmt.test, ctx):
                _collect_bound(stmt.body, ctx, names)
            _collect_bound(stmt.orelse, ctx, names)
        elif isinstance(stmt, ast.Try):
            for block in (stmt.body, stmt.orelse, stmt.finalbody):
                _collect_bound(block, ctx, names)
            for handler in stmt.handlers:
                _collect_bound(handler.body, ctx, names)
        elif isinstance(stmt, ast.For | ast.AsyncFor):
            names.update(_target_names(stmt.target))
            _collect_bound(stmt.body, ctx, names)
            _collect_bound(stmt.orelse, ctx, names)
        elif isinstance(stmt, ast.While):
            _collect_bound(stmt.body, ctx, names)
            _collect_bound(stmt.orelse, ctx, names)
        elif isinstance(stmt, ast.With | ast.AsyncWith):
            for item in stmt.items:
                if item.optional_vars is not None:
                    names.update(_target_names(item.optional_vars))
            _collect_bound(stmt.body, ctx, names)


def _target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        out: set[str] = set()
        for element in target.elts:
            out.update(_target_names(element))
        return out
    return set()
