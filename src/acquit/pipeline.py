"""Orchestration of one full analysis run.

The pure core (parsing, indexing, graph assembly, policy, selection) is wired
together here behind an imperative shell that talks to git and the filesystem.
Everything downstream of the I/O boundary stays deterministic.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from acquit import vcs
from acquit.config import AcquitConfig, load_config
from acquit.constants import PARSE_CACHE_DIR
from acquit.errors import ParseFailure
from acquit.graph.build import assemble_graph
from acquit.graph.cache import ParseCache
from acquit.graph.index import ModuleIndex, build_index, detect_roots
from acquit.graph.model import BuiltGraph, NodeKind
from acquit.graph.parse import ModuleFacts, parse_module_facts
from acquit.policy.engine import PolicyContext, PolicyOutcome, evaluate
from acquit.policy.model import ScopeKind
from acquit.pytestmap.conftree import UNPARSEABLE_MARKER, ConftestFacts, inspect_conftest
from acquit.pytestmap.discover import classify_file, discover_test_files
from acquit.pytestmap.pytestcfg import PytestConfig, load_pytest_config
from acquit.select import Decision, decide
from acquit.vcs import ChangedFile, ChangeStatus


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One analyzed tree: file listing, parsed facts, index, and its graph."""

    ref: str | None
    files: tuple[str, ...]
    kinds: Mapping[str, NodeKind]
    facts: Mapping[str, ModuleFacts]
    unparseable: tuple[str, ...]
    index: ModuleIndex
    conftest_facts: Mapping[str, ConftestFacts]
    graph: BuiltGraph


@dataclass(frozen=True, slots=True)
class SelectResult:
    """Everything one select run produced, ready for reports and explanations."""

    decision: Decision
    outcome: PolicyOutcome
    head: Snapshot
    changed: tuple[ChangedFile, ...]
    changed_kinds: Mapping[str, NodeKind]
    base_sha: str
    head_sha: str | None


def _blob_sha(content: bytes) -> str:
    # Matches git hash-object, so working-tree parses share ref cache entries.
    hasher = hashlib.sha1(usedforsecurity=False)
    hasher.update(f"blob {len(content)}\x00".encode())
    hasher.update(content)
    return hasher.hexdigest()


def _read_sources(
    ref: str | None, repo: Path, py_files: tuple[str, ...]
) -> tuple[dict[str, bytes], dict[str, str], list[str]]:
    sources: dict[str, bytes] = {}
    shas: dict[str, str] = {}
    unreadable: list[str] = []
    if ref is None:
        for path in py_files:
            try:
                content = (repo / path).read_bytes()
            except OSError:
                unreadable.append(path)
                continue
            sources[path] = content
            shas[path] = _blob_sha(content)
        return sources, shas, unreadable
    ref_shas = vcs.blob_shas(ref, repo)
    for path in py_files:
        sha = ref_shas.get(path)
        if sha is None:
            # Listed but not a blob (a submodule, say): imports unknowable.
            unreadable.append(path)
            continue
        sources[path] = vcs.read_blob(ref, path, repo)
        shas[path] = sha
    return sources, shas, unreadable


def _parse_sources(
    sources: Mapping[str, bytes], shas: Mapping[str, str], cache: ParseCache | None
) -> tuple[dict[str, ModuleFacts], list[str]]:
    facts: dict[str, ModuleFacts] = {}
    failed: list[str] = []
    for path in sorted(sources):
        sha = shas.get(path)
        cached = cache.get(sha) if cache is not None and sha is not None else None
        if cached is not None:
            # The cache is content-addressed, so a hit can carry the path of
            # another byte-identical file; rebind it to this one.
            facts[path] = cached if cached.path == path else replace(cached, path=path)
            continue
        try:
            parsed = parse_module_facts(sources[path], path)
        except ParseFailure:
            failed.append(path)
            continue
        facts[path] = parsed
        if cache is not None and sha is not None:
            cache.put(sha, parsed)
    return facts, failed


def snapshot_tree(
    ref: str | None,
    repo: Path,
    acquit_config: AcquitConfig,
    pytest_config: PytestConfig,
    cache: ParseCache | None,
) -> Snapshot:
    """Snapshot the tree at ref, or the working tree when ref is None."""
    files = vcs.list_files(ref, repo)
    py_files = tuple(path for path in files if path.endswith(".py"))
    sources, shas, unreadable = _read_sources(ref, repo, py_files)
    facts, failed = _parse_sources(sources, shas, cache)
    unparseable = tuple(sorted(unreadable + failed))

    tests = frozenset(discover_test_files(files, pytest_config))
    kinds = {path: classify_file(path, pytest_config, tests) for path in files}
    index = build_index(py_files, detect_roots(files, acquit_config.roots or None))

    conftest_facts: dict[str, ConftestFacts] = {}
    for path in files:
        if kinds[path] is not NodeKind.CONFTEST:
            continue
        source = sources.get(path)
        if source is None:
            conftest_facts[path] = ConftestFacts(
                path=path, collection_altering=(UNPARSEABLE_MARKER,), pytest_plugins=()
            )
        else:
            conftest_facts[path] = inspect_conftest(source, path)

    graph = assemble_graph(files, kinds, facts, unparseable, index, conftest_facts, pytest_config)
    return Snapshot(
        ref=ref,
        files=files,
        kinds=kinds,
        facts=facts,
        unparseable=unparseable,
        index=index,
        conftest_facts=conftest_facts,
        graph=graph,
    )


def snapshot_working_tree(cwd: Path) -> Snapshot:
    """Snapshot the working tree of the repository containing cwd."""
    repo = vcs.repo_root(cwd)
    return snapshot_tree(
        None,
        repo,
        load_config(repo),
        load_pytest_config(repo),
        ParseCache(repo / PARSE_CACHE_DIR),
    )


def _classify_changed(
    changed: tuple[ChangedFile, ...],
    kinds: Mapping[str, NodeKind],
    pytest_config: PytestConfig,
) -> Mapping[str, NodeKind]:
    out: dict[str, NodeKind] = {}
    for change in changed:
        kind = kinds.get(change.path)
        if kind is None:
            # Deleted paths are absent at head; classify them like the rules do.
            tests = frozenset(discover_test_files((change.path,), pytest_config))
            kind = classify_file(change.path, pytest_config, tests)
        out[change.path] = kind
    return out


def run_select(base: str, head: str | None, cwd: Path) -> SelectResult:
    """Run the full selection pipeline for one diff and return its outcome."""
    repo = vcs.repo_root(cwd)
    acquit_config = load_config(repo)
    pytest_config = load_pytest_config(repo)
    changed = vcs.changed_files(base, head, repo)
    cache = ParseCache(repo / PARSE_CACHE_DIR)
    head_snapshot = snapshot_tree(head, repo, acquit_config, pytest_config, cache)

    ctx = PolicyContext(
        changed=changed,
        kinds=head_snapshot.kinds,
        facts=head_snapshot.facts,
        conftest_facts=head_snapshot.conftest_facts,
        unparseable=head_snapshot.unparseable,
        index=head_snapshot.index,
        pytest_config=pytest_config,
        config=acquit_config,
    )
    outcome = evaluate(ctx)

    needs_base = any(
        change.status in (ChangeStatus.DELETED, ChangeStatus.RENAMED) for change in changed
    )
    has_global = any(finding.scope.kind is ScopeKind.GLOBAL for finding in outcome.findings)
    base_graph: BuiltGraph | None = None
    if needs_base and not has_global:
        # The base graph reuses the head pytest and acquit config: sound, because
        # a changed config fires a GLOBAL rule and selective mode is never
        # reached when configs differ.
        base_graph = snapshot_tree(base, repo, acquit_config, pytest_config, cache).graph

    decision = decide(head_snapshot.graph, base_graph, changed, outcome.findings)
    return SelectResult(
        decision=decision,
        outcome=outcome,
        head=head_snapshot,
        changed=changed,
        changed_kinds=_classify_changed(changed, head_snapshot.kinds, pytest_config),
        base_sha=vcs.resolve_sha(base, repo),
        head_sha=vcs.resolve_sha(head, repo) if head is not None else None,
    )
