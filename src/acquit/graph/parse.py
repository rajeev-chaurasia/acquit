"""Single-pass extraction of import facts from one module's source.

One AST walk collects everything the graph builder needs: import statements,
literal dynamic imports, taint suspects, PEP 562 lazy attribute hooks, and
pytest_plugins declarations. Conditional imports (if, try, TYPE_CHECKING) are
collected unconditionally; that over-approximation is deliberate and sound.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from acquit.errors import ParseFailure

_DYNAMIC_IMPORT_CALLEES = frozenset({"__import__", "import_module"})
_IMPORT_MODULE = "import_module"
_SYS_PATH_METHODS = frozenset({"append", "insert", "extend"})
_EXEC_EVAL_NAMES = frozenset({"exec", "eval", "compile"})
_SITE_PATH_CALLEES = frozenset({"addsitedir", "extend_path"})
_SYS_MODULES_METHODS = frozenset({"get", "setdefault", "pop"})


class SuspectKind(StrEnum):
    """Constructs whose dependencies cannot be known statically."""

    NON_LITERAL_DYNAMIC_IMPORT = "non-literal-dynamic-import"
    # In a function body: runs only if something calls it at runtime.
    SYS_PATH_MUTATION = "sys-path-mutation"
    # At module level or in a class body: executes whenever the module is imported.
    SYS_PATH_MUTATION_IMPORT_TIME = "sys-path-mutation-import-time"
    EXEC_EVAL = "exec-eval"
    LAZY_MODULE_GETATTR = "lazy-module-getattr"


@dataclass(frozen=True, slots=True)
class ImportStmt:
    """One import statement, normalized.

    module is None for plain `import a.b` (the names carry the targets); for
    from-imports it is the source module, "" for pure-relative `from . import x`.
    """

    module: str | None
    names: tuple[str, ...]
    level: int
    is_star: bool


@dataclass(frozen=True, slots=True)
class Suspect:
    """One occurrence of a construct that taints the module."""

    kind: SuspectKind
    lineno: int


@dataclass(frozen=True, slots=True)
class ModuleFacts:
    """Everything the graph builder needs from one parsed module."""

    path: str
    imports: tuple[ImportStmt, ...]
    # Absolute dotted names only; relative literals resolve here or taint.
    dyn_literal_imports: tuple[str, ...]
    suspects: tuple[Suspect, ...]
    defines_module_getattr: bool
    pytest_plugins_decl: tuple[str, ...]


def parse_module_facts(source: bytes, path: str) -> ModuleFacts:
    """Extract facts from one module. Raises ParseFailure on unparseable source."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        raise ParseFailure(f"{path}: {exc}") from exc
    visitor = _FactsVisitor()
    visitor.visit(tree)
    return ModuleFacts(
        path=path,
        imports=tuple(visitor.imports),
        dyn_literal_imports=tuple(visitor.dyn_literal_imports),
        suspects=tuple(visitor.suspects),
        defines_module_getattr=visitor.defines_module_getattr,
        pytest_plugins_decl=tuple(visitor.pytest_plugins_decl),
    )


def _dotted_name(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_sys_path(node: ast.expr) -> bool:
    dotted = _dotted_name(node)
    return dotted is not None and (dotted == "sys.path" or dotted.endswith(".sys.path"))


def _is_sys_modules(node: ast.expr) -> bool:
    dotted = _dotted_name(node)
    return dotted is not None and (dotted == "sys.modules" or dotted.endswith(".sys.modules"))


def _flat_targets(target: ast.expr) -> Iterator[ast.expr]:
    if isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            yield from _flat_targets(element)
    elif isinstance(target, ast.Starred):
        yield from _flat_targets(target.value)
    else:
        yield target


def _string_literals(node: ast.expr | None) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return tuple(
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        )
    return ()


def _package_argument(node: ast.Call) -> str | None:
    # import_module(name, package): the anchor a relative name resolves against.
    value: ast.expr | None = node.args[1] if len(node.args) > 1 else None
    for keyword in node.keywords:
        if keyword.arg == "package":
            value = keyword.value
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


def _is_mutation_target(target: ast.expr) -> bool:
    if isinstance(target, ast.Name):
        return target.id == "__path__"
    if isinstance(target, ast.Attribute):
        return target.attr == "__path__" or _is_sys_path(target)
    if isinstance(target, ast.Subscript):
        return _is_sys_path(target.value)
    return False


class _FactsVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: list[ImportStmt] = []
        self.dyn_literal_imports: list[str] = []
        self.suspects: list[Suspect] = []
        self.defines_module_getattr = False
        self.pytest_plugins_decl: list[str] = []
        # Depth gates only the module-level facts (PEP 562 hook, pytest_plugins);
        # imports and suspects are collected at every depth. Function depth is
        # tracked separately: class bodies run at import, function bodies do not.
        self._scope_depth = 0
        self._function_depth = 0

    def _suspect(self, kind: SuspectKind, node: ast.stmt | ast.expr) -> None:
        self.suspects.append(Suspect(kind=kind, lineno=node.lineno))

    def _sys_path_kind(self) -> SuspectKind:
        if self._function_depth > 0:
            return SuspectKind.SYS_PATH_MUTATION
        return SuspectKind.SYS_PATH_MUTATION_IMPORT_TIME

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.append(
            ImportStmt(
                module=None,
                names=tuple(alias.name for alias in node.names),
                level=0,
                is_star=False,
            )
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        names = tuple(alias.name for alias in node.names)
        self.imports.append(
            ImportStmt(
                module=node.module if node.module is not None else "",
                names=names,
                level=node.level,
                is_star="*" in names,
            )
        )

    def visit_Call(self, node: ast.Call) -> None:
        callee = _dotted_name(node.func)
        last = callee.rsplit(".", 1)[-1] if callee is not None else None
        if last in _DYNAMIC_IMPORT_CALLEES:
            # Only import_module takes a package anchor for relative names.
            package = _package_argument(node) if last == _IMPORT_MODULE else None
            self._record_dynamic_import(node, package)
        elif self._is_sys_path_call(node) or last in _SITE_PATH_CALLEES:
            self._suspect(self._sys_path_kind(), node)
        elif isinstance(node.func, ast.Name) and node.func.id in _EXEC_EVAL_NAMES:
            self._suspect(SuspectKind.EXEC_EVAL, node)
        elif self._is_sys_modules_access(node):
            self._record_dynamic_import(node, None)
        self.generic_visit(node)

    @staticmethod
    def _is_sys_modules_access(node: ast.Call) -> bool:
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr in _SYS_MODULES_METHODS
            and _is_sys_modules(func.value)
        )

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # Reading sys.modules acquires a module just like a dynamic import does.
        if _is_sys_modules(node.value):
            key = node.slice
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and not key.value.startswith(".")
            ):
                self.dyn_literal_imports.append(key.value)
            else:
                self._suspect(SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, node)
        self.generic_visit(node)

    def _record_dynamic_import(self, node: ast.Call, package: str | None) -> None:
        first = node.args[0] if node.args else None
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            self._suspect(SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, node)
            return
        target = first.value
        if target.startswith("."):
            # A relative name resolves against runtime package state unless
            # the package anchor is itself a usable literal.
            resolved = _resolve_relative_target(target, package)
            if resolved is None:
                self._suspect(SuspectKind.NON_LITERAL_DYNAMIC_IMPORT, node)
                return
            target = resolved
        self.dyn_literal_imports.append(target)

    @staticmethod
    def _is_sys_path_call(node: ast.Call) -> bool:
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr in _SYS_PATH_METHODS
            and _is_sys_path(func.value)
        )

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            for leaf in _flat_targets(target):
                self._handle_binding(leaf, node.value, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._handle_binding(node.target, node.value, node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if _is_mutation_target(node.target):
            self._suspect(self._sys_path_kind(), node)
        self.generic_visit(node)

    def _handle_binding(self, target: ast.expr, value: ast.expr | None, stmt: ast.stmt) -> None:
        if _is_mutation_target(target):
            self._suspect(self._sys_path_kind(), stmt)
            return
        if self._scope_depth > 0 or not isinstance(target, ast.Name):
            return
        if target.id == "pytest_plugins":
            self.pytest_plugins_decl.extend(_string_literals(value))
        elif target.id == "__getattr__":
            self.defines_module_getattr = True
            self._suspect(SuspectKind.LAZY_MODULE_GETATTR, stmt)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter_definition(node)

    def _enter_definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # A def-form PEP 562 hook is not a suspect by itself: its body is visited
        # like any other code, so static imports become edges and only genuinely
        # dynamic constructs inside it taint. The opaque assignment form stays a
        # suspect because there is no body to inspect.
        if self._scope_depth == 0 and node.name == "__getattr__":
            self.defines_module_getattr = True
        self._function_depth += 1
        self._descend(node)
        self._function_depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._descend(node)

    def _descend(self, node: ast.stmt) -> None:
        self._scope_depth += 1
        self.generic_visit(node)
        self._scope_depth -= 1
