"""Fail-closed policy model.

Skipping is only possible when no rule fires against a test. The engine's default
answer is always "run everything"; a skip requires affirmative evidence.
"""

from dataclasses import dataclass
from enum import StrEnum


class RuleId(StrEnum):
    CHANGED_RESOURCE = "R001"
    CHANGED_DEPENDENCY_MANIFEST = "R002"
    CHANGED_TEST_ENVIRONMENT = "R003"
    CHANGED_NATIVE_SOURCE = "R004"
    CHANGED_CONFTEST = "R005"
    COLLECTION_ALTERING_HOOK = "R006"
    NON_LITERAL_DYNAMIC_IMPORT = "R007"
    SYS_PATH_MUTATION = "R008"
    EXEC_EVAL = "R009"
    UNPARSEABLE_FILE = "R010"
    BROKEN_FIRST_PARTY_IMPORT = "R011"
    LAZY_MODULE_GETATTR = "R012"
    CHANGED_STUB = "R013"
    CHANGED_TEST_FILE = "R014"
    DOCTEST_MODULES = "R015"
    DIFF_UNAVAILABLE = "R016"
    CACHE_INVALID = "R017"
    INTERNAL_ERROR = "R018"


class ScopeKind(StrEnum):
    GLOBAL = "global"
    # Acts like GLOBAL once any head test reaches the subject; inert otherwise.
    GLOBAL_IF_REACHED = "global-if-reached"
    SUBTREE = "subtree"
    CLOSURE_TAINT = "closure-taint"
    SELF_TEST = "self-test"


@dataclass(frozen=True, slots=True)
class Scope:
    kind: ScopeKind
    # Directory for SUBTREE; node path for CLOSURE_TAINT, SELF_TEST, and
    # GLOBAL_IF_REACHED; None for GLOBAL.
    subject: str | None = None


@dataclass(frozen=True, slots=True)
class Finding:
    rule: RuleId
    scope: Scope
    subject: str
    reason: str
