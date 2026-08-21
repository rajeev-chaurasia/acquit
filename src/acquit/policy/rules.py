"""Policy rules: one pure function per rule, registered in evaluation order.

Each function reads the immutable PolicyContext and yields findings. Nothing
here touches the filesystem or mutates state; the engine sorts and dedupes the
combined output, so rules only need to be complete, not ordered.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Final

from acquit.graph.index import ModuleIndex
from acquit.graph.model import NodeKind
from acquit.graph.parse import SuspectKind
from acquit.graph.resolve import resolve_import
from acquit.policy.model import Finding, RuleId, Scope, ScopeKind
from acquit.pytestmap.conftree import UNPARSEABLE_MARKER
from acquit.pytestmap.discover import classify_file, discover_test_files

if TYPE_CHECKING:
    from acquit.policy.engine import PolicyContext

type RuleFn = Callable[[PolicyContext], Iterable[Finding]]

_CONFTEST: Final = "conftest.py"
_WORKFLOWS_PREFIX: Final = ".github/workflows/"

_MANIFEST_BASENAMES: Final = frozenset(
    {"pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "Pipfile.lock", "uv.lock", "poetry.lock"}
)
_MANIFEST_PATTERNS: Final = ("requirements*.txt", "constraints*.txt")

_ENVIRONMENT_BASENAMES: Final = frozenset(
    {"pytest.ini", "tox.ini", "sitecustomize.py", "usercustomize.py"}
)
_ENVIRONMENT_PATTERNS: Final = ("*.pth", "Dockerfile*")

_NATIVE_BASENAMES: Final = frozenset({"CMakeLists.txt", "Makefile", "meson.build"})
_NATIVE_SUFFIXES: Final = (".c", ".h", ".cc", ".cpp", ".hpp", ".pyx", ".pxd", ".so", ".pyd")

_GLOBAL: Final = Scope(ScopeKind.GLOBAL)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _dirname(path: str) -> str:
    head, _, _ = path.rpartition("/")
    return head


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def _changed_paths(ctx: PolicyContext) -> Iterator[str]:
    # Renames carry the head path; a deletion's recorded path is its old path.
    seen: set[str] = set()
    for change in ctx.changed:
        if change.path not in seen:
            seen.add(change.path)
            yield change.path


def _kind_at(ctx: PolicyContext, path: str) -> NodeKind:
    kind = ctx.kinds.get(path)
    if kind is not None:
        return kind
    # Deleted paths are absent at head; classify them so removals still count.
    tests = frozenset(discover_test_files((path,), ctx.pytest_config))
    return classify_file(path, ctx.pytest_config, tests)


def _resolves(dotted: str, index: ModuleIndex) -> bool:
    if dotted in index.by_dotted:
        return True
    prefix = dotted + "."
    return any(name.startswith(prefix) for name in index.by_dotted)


def changed_resource(ctx: PolicyContext) -> Iterator[Finding]:
    """R001: a changed resource file may feed any test."""
    for path in _changed_paths(ctx):
        if _kind_at(ctx, path) is not NodeKind.RESOURCE:
            continue
        if _matches_any(path, ctx.config.assume_inert):
            continue
        yield Finding(
            rule=RuleId.CHANGED_RESOURCE,
            scope=_GLOBAL,
            subject=path,
            reason=f"{path} is a resource file, and any test could read it at runtime.",
        )


def changed_dependency_manifest(ctx: PolicyContext) -> Iterator[Finding]:
    """R002: a changed dependency manifest can change the installed environment."""
    for path in _changed_paths(ctx):
        name = _basename(path)
        if name not in _MANIFEST_BASENAMES and not _matches_any(name, _MANIFEST_PATTERNS):
            continue
        yield Finding(
            rule=RuleId.CHANGED_DEPENDENCY_MANIFEST,
            scope=_GLOBAL,
            subject=path,
            reason=f"{path} is a dependency manifest, so the environment of every test may change.",
        )


def _is_test_environment(path: str) -> bool:
    if path == _CONFTEST or path.startswith(_WORKFLOWS_PREFIX):
        return True
    name = _basename(path)
    return name in _ENVIRONMENT_BASENAMES or _matches_any(name, _ENVIRONMENT_PATTERNS)


def changed_test_environment(ctx: PolicyContext) -> Iterator[Finding]:
    """R003: a change to the test environment can affect every test."""
    for path in _changed_paths(ctx):
        if not _is_test_environment(path):
            continue
        yield Finding(
            rule=RuleId.CHANGED_TEST_ENVIRONMENT,
            scope=_GLOBAL,
            subject=path,
            reason=f"{path} shapes the test environment, so every test could be affected.",
        )


def changed_native_source(ctx: PolicyContext) -> Iterator[Finding]:
    """R004: native source and build inputs are invisible to the import graph."""
    for path in _changed_paths(ctx):
        if _basename(path) not in _NATIVE_BASENAMES and not path.endswith(_NATIVE_SUFFIXES):
            continue
        yield Finding(
            rule=RuleId.CHANGED_NATIVE_SOURCE,
            scope=_GLOBAL,
            subject=path,
            reason=f"{path} is native build input that the import graph cannot see through.",
        )


def changed_conftest(ctx: PolicyContext) -> Iterator[Finding]:
    """R005: a changed conftest reconfigures every test below its directory."""
    for path in _changed_paths(ctx):
        if _basename(path) != _CONFTEST or path == _CONFTEST:
            continue
        directory = _dirname(path)
        yield Finding(
            rule=RuleId.CHANGED_CONFTEST,
            scope=Scope(ScopeKind.SUBTREE, directory),
            subject=path,
            reason=f"{path} changed, and pytest applies it to every test under {directory}/.",
        )


def _unresolved_first_party_plugins(entries: Iterable[str], index: ModuleIndex) -> Iterator[str]:
    for entry in entries:
        # A third-party top level is fine: its code arrives through the
        # environment, and dependency changes are already caught by R002.
        if entry.partition(".")[0] not in index.first_party_top_levels:
            continue
        if not _resolves(entry, index):
            yield entry


def _unresolved_plugin_finding(path: str, entry: str) -> Finding:
    return Finding(
        rule=RuleId.COLLECTION_ALTERING_HOOK,
        scope=_GLOBAL,
        subject=path,
        reason=(
            f"{path} declares pytest plugin {entry!r}, "
            "which looks first-party but does not resolve."
        ),
    )


def collection_altering_hook(ctx: PolicyContext) -> Iterator[Finding]:
    """R006: a conftest that can alter collection makes discovery untrustworthy."""
    for path, facts in ctx.conftest_facts.items():
        if UNPARSEABLE_MARKER in facts.collection_altering:
            yield Finding(
                rule=RuleId.COLLECTION_ALTERING_HOOK,
                scope=_GLOBAL,
                subject=path,
                reason=f"{path} could not be parsed, so its effect on collection is unknown.",
            )
        elif facts.collection_altering:
            names = ", ".join(facts.collection_altering)
            yield Finding(
                rule=RuleId.COLLECTION_ALTERING_HOOK,
                scope=_GLOBAL,
                subject=path,
                reason=f"{path} defines {names}, which can change which tests pytest collects.",
            )
        for entry in _unresolved_first_party_plugins(facts.pytest_plugins, ctx.index):
            yield _unresolved_plugin_finding(path, entry)
    # pytest_plugins in a test module loads plugins for the whole process.
    for path, module in ctx.facts.items():
        if ctx.kinds.get(path) is not NodeKind.TEST:
            continue
        for entry in _unresolved_first_party_plugins(module.pytest_plugins_decl, ctx.index):
            yield _unresolved_plugin_finding(path, entry)


def _tainting_suspects(
    ctx: PolicyContext, rule: RuleId, kind: SuspectKind, what: str
) -> Iterator[Finding]:
    for path, facts in ctx.facts.items():
        if not any(suspect.kind is kind for suspect in facts.suspects):
            continue
        yield Finding(
            rule=rule,
            scope=Scope(ScopeKind.CLOSURE_TAINT, path),
            subject=path,
            reason=f"{path} {what}, so its true dependencies are unknown.",
        )


def non_literal_dynamic_import(ctx: PolicyContext) -> Iterator[Finding]:
    """R007: a dynamic import with a non-literal target cannot be resolved."""
    return _tainting_suspects(
        ctx,
        RuleId.NON_LITERAL_DYNAMIC_IMPORT,
        SuspectKind.NON_LITERAL_DYNAMIC_IMPORT,
        "imports a module chosen at runtime",
    )


def sys_path_mutation(ctx: PolicyContext) -> Iterator[Finding]:
    """R008: mutating sys.path perturbs how every later import resolves.

    Scope splits on when and where the mutation executes. Import-time in a
    conftest is global because conftests execute unconditionally during
    collection. A changed plain module is also global; an unchanged one goes
    global only if some test reaches it. A function-level mutation runs only
    if called, so it taints the module's own closure; for a conftest, its scope
    edges already propagate that taint to exactly the tests under it.
    """
    changed = frozenset(_changed_paths(ctx))
    for path, facts in ctx.facts.items():
        kinds = {suspect.kind for suspect in facts.suspects}
        if SuspectKind.SYS_PATH_MUTATION_IMPORT_TIME in kinds:
            if _basename(path) == _CONFTEST:
                yield Finding(
                    rule=RuleId.SYS_PATH_MUTATION,
                    scope=_GLOBAL,
                    subject=path,
                    reason=(
                        f"{path} mutates sys.path at import time, and conftests "
                        "execute unconditionally during collection."
                    ),
                )
            elif path in changed:
                yield Finding(
                    rule=RuleId.SYS_PATH_MUTATION,
                    scope=_GLOBAL,
                    subject=path,
                    reason=(
                        f"{path} changed and mutates sys.path at import time, "
                        "so its process-wide import effects cannot be bounded."
                    ),
                )
            else:
                yield Finding(
                    rule=RuleId.SYS_PATH_MUTATION,
                    scope=Scope(ScopeKind.GLOBAL_IF_REACHED, path),
                    subject=path,
                    reason=(
                        f"{path} mutates sys.path at import time, which perturbs "
                        "every later import in the process, but only if something "
                        "imports this module during the test session."
                    ),
                )
        if SuspectKind.SYS_PATH_MUTATION in kinds:
            yield Finding(
                rule=RuleId.SYS_PATH_MUTATION,
                scope=Scope(ScopeKind.CLOSURE_TAINT, path),
                subject=path,
                reason=(
                    f"{path} mutates sys.path inside a function, making its own "
                    "dynamic behavior unknowable at runtime."
                ),
            )


def exec_eval(ctx: PolicyContext) -> Iterator[Finding]:
    """R009: exec, eval, and compile can run code the graph cannot see."""
    return _tainting_suspects(
        ctx, RuleId.EXEC_EVAL, SuspectKind.EXEC_EVAL, "calls exec, eval, or compile"
    )


def unparseable_file(ctx: PolicyContext) -> Iterator[Finding]:
    """R010: an unparseable file has unknowable imports."""
    for path in ctx.unparseable:
        yield Finding(
            rule=RuleId.UNPARSEABLE_FILE,
            scope=Scope(ScopeKind.CLOSURE_TAINT, path),
            subject=path,
            reason=f"{path} could not be parsed, so its imports are unknown.",
        )


def broken_first_party_import(ctx: PolicyContext) -> Iterator[Finding]:
    """R011: a changed file importing a missing first-party name is surfaced here."""
    for path in _changed_paths(ctx):
        facts = ctx.facts.get(path)
        if facts is None:
            continue
        broken: set[str] = set()
        for stmt in facts.imports:
            broken.update(resolve_import(stmt, path, ctx.index).broken_first_party)
        for name in sorted(broken):
            yield Finding(
                rule=RuleId.BROKEN_FIRST_PARTY_IMPORT,
                scope=Scope(ScopeKind.CLOSURE_TAINT, path),
                subject=path,
                reason=(
                    f"{path} imports {name!r}, which looks first-party "
                    "but does not resolve to any module."
                ),
            )


def lazy_module_getattr(ctx: PolicyContext) -> Iterator[Finding]:
    """R012: a module-level __getattr__ can import on attribute access."""
    return _tainting_suspects(
        ctx,
        RuleId.LAZY_MODULE_GETATTR,
        SuspectKind.LAZY_MODULE_GETATTR,
        "defines a module __getattr__ that can import lazily",
    )


def changed_stub(ctx: PolicyContext) -> Iterator[Finding]:
    """R013: a changed stub speaks for its sibling module, or for everything without one."""
    for path in _changed_paths(ctx):
        if not path.endswith(".pyi"):
            continue
        sibling = path.removesuffix(".pyi") + ".py"
        if sibling in ctx.kinds:
            scope = Scope(ScopeKind.CLOSURE_TAINT, sibling)
            reason = f"{path} is the stub for {sibling}, so its dependents may be affected."
        else:
            scope = _GLOBAL
            reason = f"{path} is a stub with no sibling module, so its reach cannot be modeled."
        yield Finding(rule=RuleId.CHANGED_STUB, scope=scope, subject=path, reason=reason)


def changed_test_file(ctx: PolicyContext) -> Iterator[Finding]:
    """R014: a changed test always runs itself; a changed conftest runs its subtree."""
    for path in _changed_paths(ctx):
        kind = _kind_at(ctx, path)
        if kind is NodeKind.TEST:
            yield Finding(
                rule=RuleId.CHANGED_TEST_FILE,
                scope=Scope(ScopeKind.SELF_TEST, path),
                subject=path,
                reason=f"{path} is a test file and must run because it changed.",
            )
        elif kind is NodeKind.CONFTEST:
            scope = _GLOBAL if path == _CONFTEST else Scope(ScopeKind.SUBTREE, _dirname(path))
            yield Finding(
                rule=RuleId.CHANGED_TEST_FILE,
                scope=scope,
                subject=path,
                reason=f"{path} is a conftest, so the tests it configures must run.",
            )


def doctest_modules(ctx: PolicyContext) -> Iterator[Finding]:
    """R015: --doctest-modules runs tests inside source modules, beyond static discovery."""
    if not ctx.pytest_config.doctest_modules:
        return
    source = ctx.pytest_config.source or "the pytest configuration"
    yield Finding(
        rule=RuleId.DOCTEST_MODULES,
        scope=_GLOBAL,
        subject=source,
        reason=(
            f"{source} enables --doctest-modules, so doctests execute inside "
            "source modules and static test discovery cannot bound them yet."
        ),
    )


ALL_RULES: Final[tuple[RuleFn, ...]] = (
    changed_resource,
    changed_dependency_manifest,
    changed_test_environment,
    changed_native_source,
    changed_conftest,
    collection_altering_hook,
    non_literal_dynamic_import,
    sys_path_mutation,
    exec_eval,
    unparseable_file,
    broken_first_party_import,
    lazy_module_getattr,
    changed_stub,
    changed_test_file,
    doctest_modules,
)
