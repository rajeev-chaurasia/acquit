"""The study's safety check: no changed test may hide inside a skipped file.

C is the set of tests whose normalized outcome set differs between base and
head, including tests that only exist at head, minus an explicit quarantine
of known-flaky nodes. S is the set of files acquit skipped. The study claim
is simple: files(C) never intersects S, and no head-only test file is in S.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from acquit.study.outcomes import Outcome, file_of


@dataclass(frozen=True, slots=True)
class SafetyResult:
    """One PR's safety verdict, recorded verbatim into the result file."""

    changed_outcomes: tuple[str, ...]
    unsafe_skips: tuple[str, ...]
    new_tests_selected: bool

    @property
    def safe(self) -> bool:
        return not self.unsafe_skips and self.new_tests_selected


def parse_quarantine(text: str) -> frozenset[str]:
    """One normalized node id per line; blank lines and # comments ignored."""
    nodes: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            nodes.add(stripped)
    return frozenset(nodes)


def changed_tests(
    base: Mapping[str, frozenset[Outcome]],
    head: Mapping[str, frozenset[Outcome]],
    quarantine: frozenset[str],
) -> frozenset[str]:
    """Head tests whose outcome set differs from base, head-only included.

    Tests that vanished between base and head are not counted: a removed test
    has nothing left to run, so skipping its file proves nothing either way.
    """
    changed = {node for node, outcomes in head.items() if base.get(node) != outcomes}
    return frozenset(changed - quarantine)


def check_safety(
    base: Mapping[str, frozenset[Outcome]],
    head: Mapping[str, frozenset[Outcome]],
    skip_paths: Iterable[str],
    quarantine: frozenset[str],
) -> SafetyResult:
    """Judge one PR's selection against the observed base and head outcomes.

    The quarantine only shrinks the changed set. The new-test check ignores
    it on purpose: a brand-new test inside a skipped file is unsafe even if
    someone quarantined its node id.
    """
    skipped_files = frozenset(skip_paths)
    changed = changed_tests(base, head, quarantine)
    unsafe = sorted({file_of(node) for node in changed} & skipped_files)
    head_only = {node for node in head if node not in base}
    new_in_skipped = {file_of(node) for node in head_only} & skipped_files
    return SafetyResult(
        changed_outcomes=tuple(sorted(changed)),
        unsafe_skips=tuple(unsafe),
        new_tests_selected=not new_in_skipped,
    )
