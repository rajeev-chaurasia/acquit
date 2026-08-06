"""Fail-closed policy engine.

Every rule runs on every evaluation; nothing short-circuits, so a report can
name all the reasons a suite must run in full. Waivers downgrade findings
instead of deleting them: a waived finding moves aside but stays visible.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatch

from acquit.config import AcquitConfig, Waiver
from acquit.graph.index import ModuleIndex
from acquit.graph.model import NodeKind
from acquit.graph.parse import ModuleFacts
from acquit.policy.model import Finding
from acquit.policy.rules import ALL_RULES
from acquit.pytestmap.conftree import ConftestFacts
from acquit.pytestmap.pytestcfg import PytestConfig
from acquit.vcs import ChangedFile


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Everything the rules may inspect, snapshotted for one evaluation.

    changed carries the head path for renames and the recorded (old) path for
    deletions; kinds classifies every file present at head; facts and
    conftest_facts are the parsed views of the parseable Python files.
    """

    changed: tuple[ChangedFile, ...]
    kinds: Mapping[str, NodeKind]
    facts: Mapping[str, ModuleFacts]
    conftest_facts: Mapping[str, ConftestFacts]
    unparseable: tuple[str, ...]
    index: ModuleIndex
    pytest_config: PytestConfig
    config: AcquitConfig


@dataclass(frozen=True, slots=True)
class WaivedFinding:
    """A finding downgraded by a configured waiver, kept for the report."""

    finding: Finding
    waiver: Waiver


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    """Active and waived findings, each in deterministic order."""

    findings: tuple[Finding, ...]
    waived: tuple[WaivedFinding, ...]


def _sort_key(finding: Finding) -> tuple[str, str, str, str, str]:
    # Rule id then subject is the contract; the rest only breaks ties so that
    # shuffled inputs cannot reorder otherwise-equal findings.
    return (
        finding.rule.value,
        finding.subject,
        finding.scope.kind.value,
        finding.scope.subject or "",
        finding.reason,
    )


def _matching_waiver(finding: Finding, waivers: tuple[Waiver, ...]) -> Waiver | None:
    for waiver in waivers:
        if waiver.rule == finding.rule.value and fnmatch(finding.subject, waiver.glob):
            return waiver
    return None


def evaluate(ctx: PolicyContext) -> PolicyOutcome:
    """Run every rule against ctx and split the findings into active and waived."""
    collected: set[Finding] = set()
    for rule in ALL_RULES:
        collected.update(rule(ctx))
    active: list[Finding] = []
    waived: list[WaivedFinding] = []
    for finding in sorted(collected, key=_sort_key):
        waiver = _matching_waiver(finding, ctx.config.waivers)
        if waiver is None:
            active.append(finding)
        else:
            waived.append(WaivedFinding(finding=finding, waiver=waiver))
    return PolicyOutcome(findings=tuple(active), waived=tuple(waived))
