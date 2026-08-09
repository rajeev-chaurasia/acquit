"""Re-export narrowing, the first resolver resident (ADR 0008).

A proven pure re-exporter __init__.py gets its outgoing import edges marked
INIT_REEXPORT by the builder, and every consumer importing symbols through
it gains full edges to the symbol homes, chased through chains of pure
inits. Anything unattributable, module-valued, starred, or bound by a plain
import falls back to a full fan-out that reproduces today's transitive
closure exactly, so a decline anywhere is always sound: closures never
shrink, they only gain annotation.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Final

from acquit.graph.index import ModuleIndex
from acquit.graph.parse import ImportStmt, ModuleFacts
from acquit.graph.resolve import anchored_bases, resolve_import
from acquit.graph.resolvers.checkers import BindingForm, ReexportTier
from acquit.graph.resolvers.framework import Decline, HazardSite, ResolveContext

_INIT_SUFFIX: Final = "/__init__.py"


@dataclass(frozen=True, slots=True)
class ReexportCandidate:
    """An __init__.py whose statements passed the re-exporter whitelist."""

    path: str
    facts: ModuleFacts


@dataclass(frozen=True, slots=True)
class BindingTarget:
    """Where one init-bound name points: a module, or a member of one."""

    module: str
    member: str | None


@dataclass(frozen=True, slots=True)
class PureInit:
    """The proven bound: a pure re-exporter and its attribution table.

    bindings maps each runtime-bound name to every target it may point at
    (a fail-closed union across duplicate bindings and root identities);
    local_names are bound by literal assignment in the init itself; targets
    are the first-party files the init's own statements resolve to, the
    domain of the fail-closed fan-out.
    """

    path: str
    tier: ReexportTier
    local_names: frozenset[str]
    bindings: Mapping[str, tuple[BindingTarget, ...]]
    targets: tuple[str, ...]


class ReexportResolver:
    """Recognizes package inits and proves them pure re-exporters."""

    # The narrowed claim is relational: base and head must agree, and replay
    # re-verifies the witness against two snapshots.
    axis: ClassVar[str] = "relational"

    def recognize(self, site: HazardSite) -> ReexportCandidate | None:
        # A repo-root __init__.py names no importable package. A miss here
        # is never a risk: unrecognized sites keep today's behavior.
        if not site.path.endswith(_INIT_SUFFIX):
            return None
        if site.facts.reexport.reason is not None:
            return None
        return ReexportCandidate(path=site.path, facts=site.facts)

    def prove(self, candidate: ReexportCandidate, ctx: ResolveContext) -> PureInit | Decline:
        facts = candidate.facts
        if facts.suspects or facts.dyn_literal_imports or facts.folded_dynamic_imports:
            # Only reachable through a TYPE_CHECKING body; debatable, decline.
            # A folded site is still a dynamic import, so it declines too.
            return Decline(reason="suspect-construct")
        if facts.defines_module_getattr:
            return Decline(reason="module-getattr")
        targets: set[str] = set()
        for stmt in facts.imports:
            resolution = resolve_import(stmt, candidate.path, ctx.index)
            if resolution.broken_first_party:
                return Decline(reason="broken-import")
            targets.update(dst for dst, _ in resolution.edges)
        bindings: dict[str, list[BindingTarget]] = {}
        for binding in facts.reexport.bindings:
            if binding.form is BindingForm.MODULE_IMPORT:
                bindings.setdefault(binding.name, []).append(
                    BindingTarget(module=binding.module, member=None)
                )
                continue
            bases, beyond_root = anchored_bases(
                binding.module, binding.level, candidate.path, ctx.index
            )
            if beyond_root or not bases:
                return Decline(reason="unanchored-binding")
            for base in bases:
                bindings.setdefault(binding.name, []).append(
                    BindingTarget(module=base, member=binding.member)
                )
        tier = ReexportTier.STRICT
        for star in facts.reexport.stars:
            tier = ReexportTier.STAR_ALL
            resolved = _star_source(star.module, star.level, candidate.path, ctx)
            if resolved is None:
                return Decline(reason="star-source-not-literal-all")
            source, names = resolved
            for name in names:
                bindings.setdefault(name, []).append(BindingTarget(module=source, member=name))
        return PureInit(
            path=candidate.path,
            tier=tier,
            local_names=frozenset(facts.reexport.local_names),
            bindings={name: tuple(items) for name, items in sorted(bindings.items())},
            targets=tuple(sorted(targets)),
        )


def _star_source(
    module: str, level: int, init_path: str, ctx: ResolveContext
) -> tuple[str, tuple[str, ...]] | None:
    """The star tier: one first-party source file with one literal __all__."""
    bases, beyond_root = anchored_bases(module, level, init_path, ctx.index)
    if beyond_root or not bases:
        return None
    paths: set[str] = set()
    for base in bases:
        paths.update(ctx.index.by_dotted.get(base, ()))
    if len(paths) != 1:
        return None
    (source_path,) = paths
    source_facts = ctx.facts.get(source_path)
    if source_facts is None or source_facts.reexport.all_names is None:
        return None
    return bases[0], source_facts.reexport.all_names


def reexport_consumer_edges(
    stmt: ImportStmt, importer_path: str, idx: ModuleIndex, pure: Mapping[str, PureInit]
) -> tuple[str, ...]:
    """Extra full-edge destinations one consumer statement needs across pure inits.

    From-imported names pin their homes through the attribution chase; a
    star, a plain import, or an unattributable name takes the full fan-out.
    The result is always additive: today's edges stay untouched, and every
    degraded path converges on today's closure.
    """
    if not pure:
        return ()
    out: set[str] = set()
    if stmt.module is None:
        # import a.b binds a and wires every submodule onto its parent, so
        # any pure init along the chain is attribute-accessed invisibly.
        for name in stmt.names:
            _fanout_prefixes(name, idx, pure, out)
        return tuple(sorted(out))
    bases, _ = anchored_bases(stmt.module, stmt.level, importer_path, idx)
    for base in bases:
        for init_path in idx.by_dotted.get(base, ()):
            init = pure.get(init_path)
            if init is None:
                continue
            if stmt.is_star:
                # Star binds whatever the init exports; take everything.
                _fanout(init, pure, out)
                continue
            for name in stmt.names:
                _attribute(base, init, name, idx, pure, out, frozenset({(init_path, name)}))
    return tuple(sorted(out))


def _attribute(
    base: str,
    init: PureInit,
    name: str,
    idx: ModuleIndex,
    pure: Mapping[str, PureInit],
    out: set[str],
    visited: frozenset[tuple[str, str]],
) -> None:
    """Full edges for one name imported through one pure init.

    Fail-closed union per the design doc: a local literal resolves to the
    init itself (the consumer already holds a full prefix edge there), each
    binding chases to its ultimate home, and the interpreter's submodule
    fallback always contributes when present. A name matched by nothing
    takes the init's full fan-out.
    """
    matched = name in init.local_names
    for target in init.bindings.get(name, ()):
        matched = True
        if target.member is None:
            _module_home(target.module, idx, pure, out)
        else:
            _member_home(target.module, target.member, idx, pure, out, visited)
    for sub_path in idx.by_dotted.get(f"{base}.{name}", ()):
        matched = True
        out.add(sub_path)
        sub_init = pure.get(sub_path)
        if sub_init is not None:
            # The name is a module object; attribute uses on it are
            # invisible to the parser, so its init fans out in full.
            _fanout(sub_init, pure, out)
    if not matched:
        _fanout(init, pure, out)


def _member_home(
    module: str,
    member: str,
    idx: ModuleIndex,
    pure: Mapping[str, PureInit],
    out: set[str],
    visited: frozenset[tuple[str, str]],
) -> None:
    """Chase one member of one module to its ultimate first-party homes."""
    for sub_path in idx.by_dotted.get(f"{module}.{member}", ()):
        out.add(sub_path)
        sub_init = pure.get(sub_path)
        if sub_init is not None:
            _fanout(sub_init, pure, out)
    for module_path in idx.by_dotted.get(module, ()):
        # Every module on the chain gets a full edge, chain inits included.
        out.add(module_path)
        init = pure.get(module_path)
        if init is None:
            # A plain module or an impure init: its own edges are full, so
            # transitivity covers everything below this point of the chase.
            continue
        key = (module_path, member)
        if key in visited:
            # A self or cyclic re-export. An importable program can only
            # satisfy it through the submodule fallback handled above; a
            # genuinely circular name binding raises at import for every
            # consumer, so there is no further home to pin.
            continue
        _attribute(module, init, member, idx, pure, out, visited | {key})


def _module_home(
    module: str, idx: ModuleIndex, pure: Mapping[str, PureInit], out: set[str]
) -> None:
    """Full edges for a name bound to a module object by a plain import."""
    for module_path in idx.by_dotted.get(module, ()):
        out.add(module_path)
        init = pure.get(module_path)
        if init is not None:
            _fanout(init, pure, out)


def _fanout_prefixes(
    dotted: str, idx: ModuleIndex, pure: Mapping[str, PureInit], out: set[str]
) -> None:
    parts = dotted.split(".")
    for end in range(1, len(parts) + 1):
        for path in idx.by_dotted.get(".".join(parts[:end]), ()):
            init = pure.get(path)
            if init is not None:
                _fanout(init, pure, out)


def _fanout(init: PureInit, pure: Mapping[str, PureInit], out: set[str]) -> None:
    """Full edges to everything a pure init leads to, through nested pure inits.

    The fail-closed fallback: it reproduces today's transitive closure for
    the consumer, so semantic closure equals closure and nothing narrows.
    """
    stack = [init]
    seen = {init.path}
    while stack:
        current = stack.pop()
        for target in current.targets:
            out.add(target)
            nested = pure.get(target)
            if nested is not None and nested.path not in seen:
                seen.add(nested.path)
                stack.append(nested)
