"""Constant folding for dynamic-import sites, the second resolver resident (ADR 0009).

This module owns the dynamic-import branch of fact extraction: one detector
classifies every site as a literal name, a proven fold, or a declined
suspect, so the parser and the folder can never disagree about what exists.
A fold must contain every module name the site can pass to the import
machinery in any run; extra names only grow closures, so each rule below is
justified one way: the runtime value of the folded expression is always in
the computed set, or the call raises before importing anything. Anything
outside the whitelist declines the whole site, reproducing today's taint.

``__package__`` and ``__name__`` fold symbolically so the result stays a
pure function of the module's bytes; graph construction resolves the anchors
against the module's identities under the import roots, exactly where
relative static imports resolve today, and fails closed when it cannot.
"""

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Final, NamedTuple

_DYNAMIC_IMPORT_CALLEES: Final = frozenset({"__import__", "import_module"})
_IMPORT_MODULE: Final = "import_module"
_SYS_MODULES_METHODS: Final = frozenset({"get", "setdefault", "pop"})
_READ_ONLY_DICT_METHODS: Final = frozenset({"get", "keys", "values", "items", "copy"})
_DICT_VIEW_METHODS: Final = frozenset({"keys", "values", "items"})
_WHITELISTED_BUILTIN_READS: Final = frozenset(
    {"len", "sorted", "list", "tuple", "set", "frozenset", "iter", "reversed"}
)
# Out of the v1 grammar by explicit decision: evaluating string methods at
# fold time couples the fold to the analyzing interpreter's Unicode tables.
_STRING_METHODS: Final = frozenset(
    {
        "capitalize",
        "casefold",
        "center",
        "expandtabs",
        "ljust",
        "lower",
        "lstrip",
        "removeprefix",
        "removesuffix",
        "replace",
        "rjust",
        "rstrip",
        "strip",
        "swapcase",
        "title",
        "translate",
        "upper",
        "zfill",
    }
)
_MAX_FOLD: Final = 128
_PACKAGE_ANCHOR: Final = "__package__"
_NAME_ANCHOR: Final = "__name__"

_SITE_IMPORT_MODULE: Final = "import-module"
_SITE_DUNDER_IMPORT: Final = "dunder-import"
_SITE_SYS_MODULES_METHOD: Final = "sys-modules-method"
_SITE_SYS_MODULES_SUBSCRIPT: Final = "sys-modules-subscript"


@dataclass(frozen=True, slots=True)
class AnchoredName:
    """A folded name awaiting its dunder anchor, resolved at graph construction.

    The target is the anchor's value minus its last ``ascend`` dotted
    components, with ``suffix`` appended. Resolution fails closed when the
    module has no identity under any root or the ascent runs past the anchor.
    """

    anchor: str
    ascend: int
    suffix: str


@dataclass(frozen=True, slots=True)
class FoldedImport:
    """One proven site: absolute names now, anchored forms for the builder."""

    lineno: int
    patterns: tuple[str, ...]
    names: tuple[str, ...]
    anchored: tuple[AnchoredName, ...]


@dataclass(frozen=True, slots=True)
class DeclinedImport:
    """One refused site; the reason is diagnostic, the suspect is the effect."""

    lineno: int
    reason: str


@dataclass(frozen=True, slots=True)
class DynamicImportScan:
    """Everything one module's dynamic-import sites contribute to its facts."""

    literal_names: tuple[str, ...]
    folded: tuple[FoldedImport, ...]
    declined: tuple[DeclinedImport, ...]


EMPTY_SCAN: Final = DynamicImportScan(literal_names=(), folded=(), declined=())


@dataclass(frozen=True, slots=True)
class FoldingCandidate:
    """A module whose AST carries at least one dynamic-import-shaped node."""

    tree: ast.Module


class _Decline(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Atom(NamedTuple):
    """One folded value: concrete text, or a dunder anchor plus a suffix."""

    anchor: str | None
    ascend: int
    text: str


_EMPTY_ATOM: Final = _Atom(None, 0, "")


# ---------------------------------------------------------------------------
# Detection helpers, shared with nothing: this module is the single detector.
# ---------------------------------------------------------------------------


def _dotted_name(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_sys_modules(node: ast.expr) -> bool:
    dotted = _dotted_name(node)
    return dotted is not None and (dotted == "sys.modules" or dotted.endswith(".sys.modules"))


def _package_argument(node: ast.Call) -> ast.expr | None:
    # import_module(name, package): the anchor a relative name resolves against.
    value: ast.expr | None = node.args[1] if len(node.args) > 1 else None
    for keyword in node.keywords:
        if keyword.arg == "package":
            value = keyword.value
    return value


def _literal_package(node: ast.Call) -> str | None:
    value = _package_argument(node)
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _resolve_relative_target(name: str, package: str | None) -> str | None:
    # Mirrors importlib's _resolve_name for fully literal arguments.
    if not package:
        return None
    level = len(name) - len(name.lstrip("."))
    remainder = name[level:]
    bits = package.rsplit(".", level - 1)
    if len(bits) < level:
        return None
    return f"{bits[0]}.{remainder}" if remainder else bits[0]


# ---------------------------------------------------------------------------
# Scope tables and the binding census
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StrBinding:
    value: str


@dataclass(frozen=True, slots=True)
class _TupleBinding:
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DictBinding:
    node: ast.Dict


@dataclass(frozen=True, slots=True)
class _ImportBinding:
    module: str


@dataclass(frozen=True, slots=True)
class _FromImportBinding:
    module: str | None
    member: str


@dataclass(frozen=True, slots=True)
class _ForBinding:
    iterable: ast.expr
    index: int | None
    scope: "_Scope"


@dataclass(frozen=True, slots=True)
class _OpaqueBinding:
    pass


_Binding = (
    _StrBinding
    | _TupleBinding
    | _DictBinding
    | _ImportBinding
    | _FromImportBinding
    | _ForBinding
    | _OpaqueBinding
)

_OPAQUE: Final = _OpaqueBinding()


class _Scope:
    __slots__ = ("bindings", "globals", "kind", "nonlocals", "parent")

    def __init__(self, kind: str, parent: "_Scope | None") -> None:
        self.kind = kind  # module | function | class | comprehension
        self.parent = parent
        self.bindings: dict[str, list[_Binding]] = {}
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()


@dataclass(frozen=True, slots=True)
class _Site:
    node: ast.Call | ast.Subscript
    kind: str
    scope: _Scope
    lineno: int


def _declared_names(
    body: Sequence[ast.stmt], kind: type[ast.Global] | type[ast.Nonlocal]
) -> set[str]:
    """Global or nonlocal declarations anywhere in this scope's own statements."""
    names: set[str] = set()
    stack = list(body)
    while stack:
        stmt = stack.pop()
        if isinstance(stmt, kind):
            names.update(stmt.names)
            continue
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue  # nested scopes declare for themselves
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.stmt):
                stack.append(child)
            elif isinstance(child, ast.ExceptHandler | ast.match_case):
                stack.extend(child.body)
    return names


class _Collector(ast.NodeVisitor):
    """One walk: scope tables, every binding occurrence, every dynamic site.

    The fold rules are only as sound as this census (design doc,
    counterexample 2), so every binding construct is handled explicitly and
    anything unrecognized binds opaque, which poisons the name.
    """

    def __init__(self) -> None:
        self.module = _Scope("module", None)
        self.scope = self.module
        self.sites: list[_Site] = []
        self.literal_names: list[str] = []
        self.all_bound: set[str] = set()

    # -- binding helpers

    def _bind_in(self, scope: _Scope, name: str, binding: _Binding) -> None:
        self.all_bound.add(name)
        if name in scope.globals:
            self.module.bindings.setdefault(name, []).append(binding)
            return
        if name in scope.nonlocals:
            # A nonlocal write can land in any enclosing function scope;
            # poison them all rather than model the exact target.
            outer = scope.parent
            while outer is not None:
                if outer.kind == "function":
                    outer.bindings.setdefault(name, []).append(_OPAQUE)
                outer = outer.parent
        scope.bindings.setdefault(name, []).append(binding)

    def _bind(self, name: str, binding: _Binding) -> None:
        self._bind_in(self.scope, name, binding)

    def _bind_target(self, target: ast.expr) -> None:
        """Bind only names the store actually binds; visit the rest for sites.

        A Subscript or Attribute store mutates an object and binds nothing,
        but its receiver and key still need visiting: sys.modules[k] = v is
        a detection site regardless of context.
        """
        if isinstance(target, ast.Name):
            self._bind(target.id, _OPAQUE)
        elif isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                self._bind_target(element)
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value)
        else:
            self.visit(target)

    @staticmethod
    def _classify_value(value: ast.expr, sole_name_target: bool) -> _Binding:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return _StrBinding(value.value)
        if isinstance(value, ast.Tuple) and all(
            isinstance(element, ast.Constant) and isinstance(element.value, str)
            for element in value.elts
        ):
            # Tuples only, by name: a named list or set display is a mutable
            # accumulator, the django/sqlalchemy counterexamples exactly.
            values = tuple(
                element.value
                for element in value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
            return _TupleBinding(values)
        if (
            sole_name_target
            and isinstance(value, ast.Dict)
            and all(key is not None for key in value.keys)
        ):
            # A dict bound through a multi-target assignment aliases under
            # every other target, invisibly to the mention scan.
            return _DictBinding(value)
        return _OPAQUE

    # -- statements that bind

    def visit_Assign(self, node: ast.Assign) -> None:
        sole = len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._bind(target.id, self._classify_value(node.value, sole))
            else:
                self._bind_target(target)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        # Annotations are expressions the detector must still scan.
        self.visit(node.annotation)
        if node.value is None:
            return
        if isinstance(node.target, ast.Name):
            self._bind(node.target.id, self._classify_value(node.value, True))
        else:
            self._bind_target(node.target)
        self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._bind_target(node.target)
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        # PEP 572: binds in the nearest enclosing non-comprehension scope.
        scope = self.scope
        while scope.kind == "comprehension" and scope.parent is not None:
            scope = scope.parent
        if isinstance(node.target, ast.Name):
            self._bind_in(scope, node.target.id, _OPAQUE)
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._bind(target.id, _OPAQUE)
            else:
                self.visit(target)

    def _bind_for_target(self, target: ast.expr, iterable: ast.expr, iter_scope: _Scope) -> None:
        if isinstance(target, ast.Name):
            self._bind(target.id, _ForBinding(iterable, None, iter_scope))
        elif isinstance(target, ast.Tuple) and all(
            isinstance(element, ast.Name) for element in target.elts
        ):
            for position, element in enumerate(target.elts):
                if isinstance(element, ast.Name):
                    self._bind(element.id, _ForBinding(iterable, position, iter_scope))
        else:
            self._bind_target(target)

    def visit_For(self, node: ast.For) -> None:
        self._bind_for_target(node.target, node.iter, self.scope)
        self.visit(node.iter)
        for stmt in node.body + node.orelse:
            self.visit(stmt)

    visit_AsyncFor = visit_For  # type: ignore[assignment]

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars)
        for stmt in node.body:
            self.visit(stmt)

    visit_AsyncWith = visit_With  # type: ignore[assignment]

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind(node.name, _OPAQUE)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.partition(".")[0]
            self._bind(bound, _ImportBinding(alias.name))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name
            module = node.module if node.level == 0 else None
            self._bind(bound, _FromImportBinding(module, alias.name))

    def visit_Global(self, node: ast.Global) -> None:
        pass  # handled by the per-scope prescan

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        pass  # handled by the per-scope prescan

    def visit_Match(self, node: ast.Match) -> None:
        for case in node.cases:
            for sub in ast.walk(case.pattern):
                for attr in ("name", "rest"):
                    captured = getattr(sub, attr, None)
                    if isinstance(captured, str):
                        self._bind(captured, _OPAQUE)
        self.generic_visit(node)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        if isinstance(node.name, ast.Name):
            self._bind(node.name.id, _OPAQUE)
        # The alias value is lazy, but the detector has always scanned it.
        self.visit(node.value)

    # -- new scopes

    def _enter(self, kind: str) -> _Scope:
        self.scope = _Scope(kind, self.scope)
        return self.scope

    def _exit(self) -> None:
        parent = self.scope.parent
        if parent is not None:
            self.scope = parent

    def _bind_type_params(self, params: Sequence[ast.type_param], scope: _Scope) -> None:
        for param in params:
            captured = getattr(param, "name", None)
            if isinstance(captured, str):
                self._bind_in(scope, captured, _OPAQUE)
            self.generic_visit(param)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._bind(node.name, _OPAQUE)
        args = node.args
        for default in args.defaults + [d for d in args.kw_defaults if d is not None]:
            self.visit(default)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for argument in _all_arguments(args):
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        scope = self._enter("function")
        scope.globals = _declared_names(node.body, ast.Global)
        scope.nonlocals = _declared_names(node.body, ast.Nonlocal)
        self._bind_type_params(node.type_params, scope)
        for argument in _all_arguments(args):
            self._bind_in(scope, argument.arg, _OPAQUE)
        for stmt in node.body:
            self.visit(stmt)
        self._exit()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        args = node.args
        for default in args.defaults + [d for d in args.kw_defaults if d is not None]:
            self.visit(default)
        scope = self._enter("function")
        for argument in _all_arguments(args):
            self._bind_in(scope, argument.arg, _OPAQUE)
        self.visit(node.body)
        self._exit()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._bind(node.name, _OPAQUE)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for decorator in node.decorator_list:
            self.visit(decorator)
        scope = self._enter("class")
        # Class bodies run at import and may declare global; missing the
        # prescan here is exactly the constant-that-is-not counterexample.
        scope.globals = _declared_names(node.body, ast.Global)
        scope.nonlocals = _declared_names(node.body, ast.Nonlocal)
        self._bind_type_params(node.type_params, scope)
        for stmt in node.body:
            self.visit(stmt)
        self._exit()

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
    ) -> None:
        outer = self.scope
        self.visit(node.generators[0].iter)
        self._enter("comprehension")
        for position, generator in enumerate(node.generators):
            # The first iterable evaluates in the enclosing scope; the rest
            # evaluate inside the comprehension. The fold must resolve each
            # one where the interpreter does.
            iter_scope = outer if position == 0 else self.scope
            self._bind_for_target(generator.target, generator.iter, iter_scope)
            if position > 0:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self._exit()

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension
    visit_DictComp = _visit_comprehension

    # -- site detection

    def visit_Call(self, node: ast.Call) -> None:
        callee = _dotted_name(node.func)
        last = callee.rsplit(".", 1)[-1] if callee is not None else None
        if last in _DYNAMIC_IMPORT_CALLEES:
            package = _literal_package(node) if last == _IMPORT_MODULE else None
            kind = _SITE_IMPORT_MODULE if last == _IMPORT_MODULE else _SITE_DUNDER_IMPORT
            self._record_dynamic_import(node, package, kind)
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _SYS_MODULES_METHODS
            and _is_sys_modules(node.func.value)
        ):
            self._record_dynamic_import(node, None, _SITE_SYS_MODULES_METHOD)
        self.generic_visit(node)

    def _record_dynamic_import(self, node: ast.Call, package: str | None, kind: str) -> None:
        # Literal classification matches the parser's historical behavior
        # byte for byte; only the non-literal remainder needs proving.
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            target = first.value
            if not target.startswith("."):
                self.literal_names.append(target)
                return
            resolved = _resolve_relative_target(target, package)
            if resolved is not None:
                self.literal_names.append(resolved)
                return
        self.sites.append(_Site(node=node, kind=kind, scope=self.scope, lineno=node.lineno))

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # Reading sys.modules acquires a module just like a dynamic import does.
        if _is_sys_modules(node.value):
            key = node.slice
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and not key.value.startswith(".")
            ):
                self.literal_names.append(key.value)
            else:
                self.sites.append(
                    _Site(
                        node=node,
                        kind=_SITE_SYS_MODULES_SUBSCRIPT,
                        scope=self.scope,
                        lineno=node.lineno,
                    )
                )
        self.generic_visit(node)


def _all_arguments(args: ast.arguments) -> list[ast.arg]:
    out = args.posonlyargs + args.args + args.kwonlyargs
    if args.vararg is not None:
        out.append(args.vararg)
    if args.kwarg is not None:
        out.append(args.kwarg)
    return out


# ---------------------------------------------------------------------------
# Folding
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FoldContext:
    tree: ast.Module
    module: _Scope
    all_bound: frozenset[str]
    parents: dict[ast.AST, ast.AST] | None = None
    tags: set[str] = field(default_factory=set)
    active: set[tuple[int, str]] = field(default_factory=set)

    def parent_map(self) -> dict[ast.AST, ast.AST]:
        if self.parents is None:
            self.parents = {}
            for parent in ast.walk(self.tree):
                for child in ast.iter_child_nodes(parent):
                    self.parents[child] = parent
        return self.parents


def _resolve_scope(
    name: str, scope: _Scope, module: _Scope
) -> tuple[_Scope, list[_Binding]] | None:
    current: _Scope | None = scope
    innermost = True
    while current is not None:
        if name in current.globals:
            return (module, module.bindings.get(name, []))
        # Class scopes are invisible to references from nested scopes.
        skip = current.kind == "class" and not innermost
        if not skip and name in current.bindings:
            return (current, current.bindings[name])
        innermost = False
        current = current.parent
    return None


def _capped(atoms: Iterable[_Atom]) -> frozenset[_Atom]:
    out = frozenset(atoms)
    if len(out) > _MAX_FOLD:
        raise _Decline("explosion")
    return out


def _concat(parts: Sequence[frozenset[_Atom]]) -> frozenset[_Atom]:
    out: set[_Atom] = {_EMPTY_ATOM}
    for part in parts:
        joined: set[_Atom] = set()
        for left in out:
            for right in part:
                if right.anchor is not None:
                    if left != _EMPTY_ATOM:
                        # Dunders fold in leading position only.
                        raise _Decline("anchor-position")
                    joined.add(right)
                else:
                    joined.add(_Atom(left.anchor, left.ascend, left.text + right.text))
        if len(joined) > _MAX_FOLD:
            raise _Decline("explosion")
        out = joined
    return frozenset(out)


def _registry_escapes(name: str, ctx: _FoldContext) -> bool:
    """True when a module-level dict registry name aliases or mutates anywhere.

    Whitelist per the design doc: subscript loads, read-only dict method
    calls, membership and comparison tests, loop iterables, and arguments to
    unshadowed builtin reads. Any other mention rejects the registry; the
    scan is module-wide and textual, so shadowed locals over-decline safely.
    """
    parents = ctx.parent_map()
    for node, parent in parents.items():
        if not (isinstance(node, ast.Name) and node.id == name):
            continue
        if isinstance(node.ctx, ast.Store):
            if isinstance(parent, ast.Assign | ast.AnnAssign):
                continue  # the binding itself; single-binding is checked separately
            return True
        if isinstance(node.ctx, ast.Del):
            return True
        if isinstance(parent, ast.Subscript) and parent.value is node:
            if isinstance(parent.ctx, ast.Store | ast.Del):
                return True
            grand = parents.get(parent)
            if isinstance(grand, ast.AugAssign) and grand.target is parent:
                return True
            continue
        if isinstance(parent, ast.Attribute) and parent.value is node:
            call = parents.get(parent)
            if (
                isinstance(call, ast.Call)
                and call.func is parent
                and parent.attr in _READ_ONLY_DICT_METHODS
            ):
                continue
            return True
        if isinstance(parent, ast.Compare):
            continue
        if isinstance(parent, ast.For | ast.AsyncFor | ast.comprehension) and parent.iter is node:
            continue
        if isinstance(parent, ast.Call) and any(argument is node for argument in parent.args):
            func = parent.func
            if (
                isinstance(func, ast.Name)
                and func.id in _WHITELISTED_BUILTIN_READS | {"len"}
                and func.id not in ctx.all_bound
            ):
                continue
            return True
        return True
    return False


def _registry_dict(name: str, scope: _Scope, ctx: _FoldContext) -> ast.Dict | None:
    resolved = _resolve_scope(name, scope, ctx.module)
    if resolved is None:
        return None
    owner, bindings = resolved
    if owner is not ctx.module or len(bindings) != 1:
        return None
    binding = bindings[0]
    if not isinstance(binding, _DictBinding):
        return None
    if _registry_escapes(name, ctx):
        return None
    return binding.node


def _as_dict(node: ast.expr, scope: _Scope, ctx: _FoldContext) -> ast.Dict | None:
    if isinstance(node, ast.Dict) and all(key is not None for key in node.keys):
        return node
    if isinstance(node, ast.Name):
        return _registry_dict(node.id, scope, ctx)
    return None


def _dict_key_atoms(node: ast.Dict, scope: _Scope, ctx: _FoldContext) -> frozenset[_Atom]:
    atoms: set[_Atom] = set()
    for key in node.keys:
        if key is None:
            raise _Decline("dict-unpack")
        atoms |= _fold_expr(key, scope, ctx)
    return _capped(atoms)


def _dict_value_atoms(node: ast.Dict, scope: _Scope, ctx: _FoldContext) -> frozenset[_Atom]:
    atoms: set[_Atom] = set()
    for value in node.values:
        atoms |= _fold_expr(value, scope, ctx)
    return _capped(atoms)


def _fold_iter(
    node: ast.expr, index: int | None, scope: _Scope, ctx: _FoldContext
) -> frozenset[_Atom]:
    """The element set a loop target ranges over, per unpack position.

    index None is a plain name target. Unpacking folds only over displays of
    same-length literal tuples or dict items(): unpacking anything else can
    slice a string apart ("xy" unpacks to "x" and "y"), which no element
    union covers.
    """
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        inner = [element for element in node.elts if isinstance(element, ast.Tuple)]
        if node.elts and len(inner) == len(node.elts):
            widths = {len(element.elts) for element in inner}
            if index is None or len(widths) != 1 or index >= widths.pop():
                raise _Decline("unpack-shape")
            atoms: set[_Atom] = set()
            for element in inner:
                atoms |= _fold_expr(element.elts[index], scope, ctx)
            ctx.tags.add("loop-literal-pairs")
            return _capped(atoms)
        if index is not None:
            raise _Decline("unpack-shape")
        atoms = set()
        for item in node.elts:
            atoms |= _fold_expr(item, scope, ctx)
        ctx.tags.add("loop-literal-display")
        return _capped(atoms)
    if isinstance(node, ast.Dict):
        if index is not None:
            raise _Decline("unpack-shape")
        ctx.tags.add("loop-dict-keys")
        return _dict_key_atoms(node, scope, ctx)
    if isinstance(node, ast.Name):
        resolved = _resolve_scope(node.id, scope, ctx.module)
        if (
            resolved is not None
            and resolved[1]
            and all(isinstance(binding, _TupleBinding) for binding in resolved[1])
        ):
            if index is not None:
                raise _Decline("unpack-shape")
            atoms = set()
            for binding in resolved[1]:
                if isinstance(binding, _TupleBinding):
                    atoms.update(_Atom(None, 0, value) for value in binding.values)
            ctx.tags.add("loop-named-tuple")
            return _capped(atoms)
        registry = _registry_dict(node.id, scope, ctx)
        if registry is not None:
            if index is not None:
                raise _Decline("unpack-shape")
            ctx.tags.add("loop-registry-keys")
            return _dict_key_atoms(registry, scope, ctx)
        raise _Decline("iterable-name")
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _DICT_VIEW_METHODS:
            if node.args or node.keywords:
                raise _Decline("iterable-method-args")
            mapping = _as_dict(func.value, scope, ctx)
            if mapping is None:
                raise _Decline("iterable-method-receiver")
            ctx.tags.add(f"loop-registry-{func.attr}")
            if func.attr == "items":
                # items() yields pairs; fold only the requested slot, so
                # unfoldable values never block a keys-only consumer.
                if index == 0:
                    return _dict_key_atoms(mapping, scope, ctx)
                if index == 1:
                    return _dict_value_atoms(mapping, scope, ctx)
                raise _Decline("unpack-shape")
            if index is not None:
                raise _Decline("unpack-shape")
            if func.attr == "keys":
                return _dict_key_atoms(mapping, scope, ctx)
            return _dict_value_atoms(mapping, scope, ctx)
        # Iterator wrappers (sorted and friends) are out of the v1 grammar.
        raise _Decline("iterable-call")
    raise _Decline("iterable-" + type(node).__name__.lower())


def _fold_name(name: str, scope: _Scope, ctx: _FoldContext) -> frozenset[_Atom]:
    resolved = _resolve_scope(name, scope, ctx.module)
    if resolved is None:
        if name == _PACKAGE_ANCHOR:
            ctx.tags.add("dunder-package")
            return frozenset({_Atom(_PACKAGE_ANCHOR, 0, "")})
        if name == _NAME_ANCHOR:
            ctx.tags.add("dunder-name")
            return frozenset({_Atom(_NAME_ANCHOR, 0, "")})
        raise _Decline("unbound-name")
    owner, bindings = resolved
    if owner is ctx.module and name in (_PACKAGE_ANCHOR, _NAME_ANCHOR):
        # The import machinery pre-binds these, so even an all-literal
        # rebinding union would miss the machinery's own value.
        raise _Decline("dunder-rebound")
    if not bindings:
        raise _Decline("unbound-name")
    key = (id(owner), name)
    if key in ctx.active:
        raise _Decline("cyclic")
    ctx.active.add(key)
    try:
        if all(isinstance(binding, _StrBinding) for binding in bindings):
            # At any program point the name is unbound (NameError, no
            # import) or holds one of the literals; the union covers both.
            ctx.tags.add("name-constant" if owner.kind == "module" else "local-constant")
            return _capped(
                _Atom(None, 0, binding.value)
                for binding in bindings
                if isinstance(binding, _StrBinding)
            )
        if len(bindings) == 1 and isinstance(bindings[0], _ForBinding):
            binding = bindings[0]
            atoms = _fold_iter(binding.iterable, binding.index, binding.scope, ctx)
            ctx.tags.add("loop-variable")
            return atoms
        if len(bindings) == 1 and isinstance(bindings[0], _FromImportBinding):
            # Depending on a second file's contents breaks the
            # one-blob-one-fold cache story; explicitly out of v1.
            raise _Decline("cross-module-constant")
        raise _Decline("rebound")
    finally:
        ctx.active.discard(key)


def _fold_expr(node: ast.expr, scope: _Scope, ctx: _FoldContext) -> frozenset[_Atom]:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return frozenset({_Atom(None, 0, node.value)})
        raise _Decline("non-str-constant")
    if isinstance(node, ast.Name):
        return _fold_name(node.id, scope, ctx)
    if isinstance(node, ast.JoinedStr):
        parts: list[frozenset[_Atom]] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(frozenset({_Atom(None, 0, str(value.value))}))
            elif isinstance(value, ast.FormattedValue):
                if value.conversion != -1 or value.format_spec is not None:
                    # Conversions and format specs call into formatting
                    # protocols the folder would have to simulate.
                    raise _Decline("fstring-spec")
                parts.append(_fold_expr(value.value, scope, ctx))
            else:
                raise _Decline("fstring-part")
        ctx.tags.add("f-string")
        return _concat(parts)
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            ctx.tags.add("concat")
            return _concat([_fold_expr(node.left, scope, ctx), _fold_expr(node.right, scope, ctx)])
        if isinstance(node.op, ast.Mod):
            raise _Decline("str-percent")  # out of the v1 grammar
        raise _Decline("binop")
    if isinstance(node, ast.IfExp):
        ctx.tags.add("ifexp")
        return _capped(_fold_expr(node.body, scope, ctx) | _fold_expr(node.orelse, scope, ctx))
    if isinstance(node, ast.BoolOp):
        atoms: set[_Atom] = set()
        for value in node.values:
            atoms |= _fold_expr(value, scope, ctx)
        ctx.tags.add("boolop")
        return _capped(atoms)
    if isinstance(node, ast.Subscript):
        mapping = _as_dict(node.value, scope, ctx)
        if mapping is not None:
            # A plain dict returns one of the display's values or raises
            # KeyError and imports nothing, so the key needs no bound.
            ctx.tags.add("registry-subscript")
            return _dict_value_atoms(mapping, scope, ctx)
        raise _Decline("subscript")
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "get":
                mapping = _as_dict(func.value, scope, ctx)
                if mapping is not None and not node.keywords and len(node.args) in (1, 2):
                    values = _dict_value_atoms(mapping, scope, ctx)
                    if len(node.args) == 2:
                        values = values | _fold_expr(node.args[1], scope, ctx)
                    ctx.tags.add("registry-get")
                    return _capped(values)
                raise _Decline("get-receiver")
            if func.attr in ("format", "format_map"):
                raise _Decline("str-format")  # out of the v1 grammar
            if func.attr == "join":
                raise _Decline("str-join")  # out of the v1 grammar
            if func.attr in _STRING_METHODS:
                raise _Decline("string-method")
            raise _Decline("method-call")
        raise _Decline("call")
    raise _Decline(type(node).__name__.lower())


# ---------------------------------------------------------------------------
# Callee provenance, required before any folding applies
# ---------------------------------------------------------------------------


def _prove_callee(site: _Site, ctx: _FoldContext) -> None:
    """The detector's name-based over-approximation is sound for tainting
    and would be unsound to fold; a look-alike callee declines here."""
    if site.kind in (_SITE_SYS_MODULES_METHOD, _SITE_SYS_MODULES_SUBSCRIPT):
        node = site.node
        if isinstance(node, ast.Call):
            func = node.func
            receiver = func.value if isinstance(func, ast.Attribute) else func
        else:
            receiver = node.value
        if _dotted_name(receiver) != "sys.modules":
            raise _Decline("provenance-receiver")
        resolved = _resolve_scope("sys", site.scope, ctx.module)
        if (
            resolved is None
            or not resolved[1]
            or not all(
                isinstance(binding, _ImportBinding) and binding.module.partition(".")[0] == "sys"
                for binding in resolved[1]
            )
        ):
            raise _Decline("provenance-sys")
        return
    node = site.node
    if not isinstance(node, ast.Call):
        raise _Decline("provenance-callee")
    func = node.func
    if isinstance(func, ast.Name):
        if func.id == "__import__":
            # Unbound everywhere in the module means the builtin; one
            # shadowing binding anywhere declines every site.
            if "__import__" in ctx.all_bound:
                raise _Decline("provenance-shadowed")
            return
        resolved = _resolve_scope(func.id, site.scope, ctx.module)
        if (
            resolved is None
            or not resolved[1]
            or not all(
                isinstance(binding, _FromImportBinding)
                and binding.module == "importlib"
                and binding.member == _IMPORT_MODULE
                for binding in resolved[1]
            )
        ):
            raise _Decline("provenance-import-module")
        return
    if isinstance(func, ast.Attribute) and func.attr == _IMPORT_MODULE:
        base = func.value
        if isinstance(base, ast.Name):
            resolved = _resolve_scope(base.id, site.scope, ctx.module)
            if (
                resolved is not None
                and resolved[1]
                and all(
                    isinstance(binding, _ImportBinding)
                    and binding.module.partition(".")[0] == "importlib"
                    for binding in resolved[1]
                )
            ):
                return
        raise _Decline("provenance-receiver")
    raise _Decline("provenance-callee")


# ---------------------------------------------------------------------------
# Per-site driver
# ---------------------------------------------------------------------------


def _name_argument(site: _Site) -> ast.expr:
    node = site.node
    if isinstance(node, ast.Subscript):
        return node.slice
    if node.args:
        first = node.args[0]
        if isinstance(first, ast.Starred):
            raise _Decline("starred-argument")
        return first
    for keyword in node.keywords:
        if keyword.arg == "name":
            return keyword.value
    raise _Decline("no-argument")


def _relative_atom(package: _Atom, level: int, remainder: str) -> _Atom:
    if package.anchor is not None:
        if package.ascend != 0 or package.text != "":
            raise _Decline("relative-package-shape")
        suffix = f".{remainder}" if remainder else ""
        return _Atom(package.anchor, level - 1, suffix)
    resolved = _resolve_relative_target("." * level + remainder, package.text)
    if resolved is None:
        raise _Decline("relative-unresolvable")
    return _Atom(None, 0, resolved)


def _apply_relative(atoms: frozenset[_Atom], site: _Site, ctx: _FoldContext) -> frozenset[_Atom]:
    relative = {atom for atom in atoms if atom.anchor is None and atom.text.startswith(".")}
    if not relative:
        return atoms
    if site.kind != _SITE_IMPORT_MODULE or not isinstance(site.node, ast.Call):
        raise _Decline("relative-key")
    package_expr = _package_argument(site.node)
    if package_expr is None:
        # import_module raises without a package argument; declining keeps
        # today's suspect rather than modeling the raise.
        raise _Decline("relative-no-package")
    package_atoms = _fold_expr(package_expr, site.scope, ctx)
    out = set(atoms) - relative
    for atom in relative:
        level = len(atom.text) - len(atom.text.lstrip("."))
        remainder = atom.text[level:]
        for package in package_atoms:
            out.add(_relative_atom(package, level, remainder))
    ctx.tags.add("relative")
    return _capped(out)


def _apply_dunder_import(
    site: _Site, atoms: frozenset[_Atom], ctx: _FoldContext
) -> frozenset[_Atom]:
    node = site.node
    if not isinstance(node, ast.Call):
        raise _Decline("provenance-callee")
    level_expr: ast.expr | None = node.args[4] if len(node.args) > 4 else None
    fromlist_expr: ast.expr | None = node.args[3] if len(node.args) > 3 else None
    for keyword in node.keywords:
        if keyword.arg == "level":
            level_expr = keyword.value
        elif keyword.arg == "fromlist":
            fromlist_expr = keyword.value
        elif keyword.arg not in ("globals", "locals", "name"):
            raise _Decline("dunder-import-kwargs")
    if level_expr is not None and not (
        isinstance(level_expr, ast.Constant) and level_expr.value == 0
    ):
        # A nonzero level resolves against runtime globals.
        raise _Decline("dunder-import-level")
    if fromlist_expr is None:
        return atoms
    if not isinstance(fromlist_expr, ast.Tuple | ast.List):
        raise _Decline("dunder-import-fromlist")
    if not fromlist_expr.elts:
        return atoms
    # __import__ with a fromlist imports submodules named there.
    extended = set(atoms)
    for element in fromlist_expr.elts:
        for entry in _fold_expr(element, site.scope, ctx):
            if entry.anchor is not None:
                raise _Decline("fromlist-anchor")
            if not entry.text.isidentifier():
                # "*" pulls in whatever __all__ names; nothing bounds it.
                raise _Decline("fromlist-entry")
            for atom in atoms:
                extended.add(_Atom(atom.anchor, atom.ascend, f"{atom.text}.{entry.text}"))
    ctx.tags.add("fromlist")
    return _capped(extended)


def _fold_site(site: _Site, ctx: _FoldContext) -> FoldedImport | DeclinedImport:
    ctx.tags = set()
    ctx.active = set()
    try:
        _prove_callee(site, ctx)
        atoms = _fold_expr(_name_argument(site), site.scope, ctx)
        atoms = _apply_relative(atoms, site, ctx)
        if site.kind == _SITE_DUNDER_IMPORT:
            atoms = _apply_dunder_import(site, atoms, ctx)
        names: set[str] = set()
        anchored: set[AnchoredName] = set()
        for atom in atoms:
            if atom.anchor is None:
                if atom.text.startswith("."):
                    raise _Decline("relative-residue")
                names.add(atom.text)
            else:
                anchored.add(AnchoredName(anchor=atom.anchor, ascend=atom.ascend, suffix=atom.text))
        return FoldedImport(
            lineno=site.lineno,
            patterns=tuple(sorted(ctx.tags)),
            names=tuple(sorted(names)),
            anchored=tuple(sorted(anchored, key=lambda a: (a.anchor, a.ascend, a.suffix))),
        )
    except _Decline as decline:
        return DeclinedImport(lineno=site.lineno, reason=decline.reason)
    except RecursionError:
        return DeclinedImport(lineno=site.lineno, reason="recursion")


class FoldingResolver:
    """Recognizes dynamic-import sites and proves finite bounds for them.

    The proof is a pure function of one module's bytes: recognize matches
    any module carrying a dynamic-import-shaped node, prove classifies every
    site as a literal, a fold, or a decline. Replay re-derives each fold
    when it rebuilds the snapshot, with no recorded evidence beyond the
    graph itself.
    """

    # The seam's axis: this proof holds within a single revision's graph
    # construction, where narrowing's is relational across two revisions.
    axis: ClassVar[str] = "per-revision"

    def recognize(self, tree: ast.Module) -> FoldingCandidate | None:
        """Cheap presence probe so most modules skip the scope walk entirely."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                callee = _dotted_name(node.func)
                last = callee.rsplit(".", 1)[-1] if callee is not None else None
                if last in _DYNAMIC_IMPORT_CALLEES:
                    return FoldingCandidate(tree=tree)
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in _SYS_MODULES_METHODS
                    and _is_sys_modules(func.value)
                ):
                    return FoldingCandidate(tree=tree)
            elif isinstance(node, ast.Subscript) and _is_sys_modules(node.value):
                return FoldingCandidate(tree=tree)
        return None

    def prove(self, candidate: FoldingCandidate) -> DynamicImportScan:
        """Classify every dynamic-import site: literal, folded, or declined."""
        collector = _Collector()
        collector.visit(candidate.tree)
        ctx = _FoldContext(
            tree=candidate.tree,
            module=collector.module,
            all_bound=frozenset(collector.all_bound),
        )
        folded: list[FoldedImport] = []
        declined: list[DeclinedImport] = []
        for site in collector.sites:
            outcome = _fold_site(site, ctx)
            if isinstance(outcome, FoldedImport):
                folded.append(outcome)
            else:
                declined.append(outcome)
        return DynamicImportScan(
            literal_names=tuple(collector.literal_names),
            folded=tuple(folded),
            declined=tuple(declined),
        )


# Fact extraction imports this singleton directly: the registry module sits
# above framework.py, which imports parse.py, so parse.py cannot reach it
# without a cycle. registry.py re-exports the same instance as the resident.
FOLDING_RESOLVER: Final = FoldingResolver()
