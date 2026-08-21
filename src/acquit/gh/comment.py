"""The sticky PR comment: render a report as markdown and upsert it in place.

Rendering is a pure function over the report document. The upsert talks to
the GitHub REST API through an injectable opener so tests never touch the
network. Any failure on any path is a warning on stderr and a zero exit;
a missing comment must never fail CI.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import urllib.request
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from acquit import __version__
from acquit.constants import COMMENT_MARKER
from acquit.errors import AcquitError, ExitCode
from acquit.report import ReportDigest, SelectionMode, digest_report

_TIMEOUT_SECONDS: Final = 10.0
_PER_PAGE: Final = 100
# A looping or hostile API must not hold CI hostage; stop paging here.
_MAX_PAGES: Final = 50
_API_VERSION: Final = "2022-11-28"
_DEFAULT_API_URL: Final = "https://api.github.com"

_REPO_PATTERN: Final = re.compile(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")
_PR_REF_PATTERN: Final = re.compile(r"refs/pull/(\d+)/(?:merge|head)")

# Longer than this and the skipped-files table starts folded shut.
_COLLAPSE_OVER: Final = 20
_TOP_REASONS: Final = 3

# Most specific first, so docs/changes.md is attributed to docs/**, not *.md.
_DOCS_PATTERNS: Final = ("docs/**", "CHANGES*", "CHANGELOG*", "*.md", "*.rst")


def _cell(text: str) -> str:
    """Make arbitrary text safe inside a one-line markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _human_seconds(seconds: float) -> str:
    if seconds < 90:
        count = max(1, round(seconds))
        unit = "second" if count == 1 else "seconds"
        return f"about {count} {unit}"
    return f"about {round(seconds / 60)} minutes"


def _top(counted: Counter[str]) -> str:
    ranked = sorted(counted.items(), key=lambda item: (-item[1], item[0]))[:_TOP_REASONS]
    parts = [f"`{_cell(name)}` ({count})" for name, count in ranked]
    if len(counted) > _TOP_REASONS:
        parts.append(f"and {len(counted) - _TOP_REASONS} more")
    return ", ".join(parts)


def _entries(tests: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    raw = tests.get(key)
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, Mapping)]


def _render_selective(digest: ReportDigest, tests: Mapping[str, Any]) -> list[str]:
    lines = [f"## Acquit: {digest.skipped} of {digest.total} test files provably unaffected", ""]
    saved = digest.estimated_seconds_saved
    if saved is not None and saved > 0:
        lines += [f"Estimated time saved: {_human_seconds(saved)}.", ""]

    skipped = _entries(tests, "skipped")
    plural = "file" if len(skipped) == 1 else "files"
    open_attr = "" if len(skipped) > _COLLAPSE_OVER else " open"
    lines += [
        f"<details{open_attr}>",
        f"<summary>{len(skipped)} skipped test {plural}, each backed by a verified "
        "witness</summary>",
        "",
        "| Skipped test file | Witness |",
        "| --- | --- |",
    ]
    for entry in skipped:
        path = _cell(str(entry.get("path", "?")))
        witness = _cell(str(entry.get("witness", "?")))
        lines.append(f"| `{path}` | `{witness}` |")
    lines += ["", "</details>", ""]

    lines.append(f"Still running: {digest.selected} selected, {digest.always_run} always-run.")
    selected_reasons: Counter[str] = Counter()
    for entry in _entries(tests, "selected"):
        reasons = entry.get("reasons")
        if isinstance(reasons, list):
            selected_reasons.update(str(reason) for reason in reasons)
    if selected_reasons:
        lines.append(f"- Selected because: {_top(selected_reasons)}")
    always_reasons = Counter(
        str(entry.get("finding", "?")) for entry in _entries(tests, "always_run")
    )
    if always_reasons:
        lines.append(f"- Always-run because: {_top(always_reasons)}")
    return lines


def _docs_pattern_for(subject: str) -> str | None:
    for pattern in _DOCS_PATTERNS:
        if pattern == "docs/**":
            if subject.startswith("docs/"):
                return pattern
        elif fnmatch.fnmatchcase(subject, pattern):
            return pattern
    return None


def _docs_nudge(findings: Sequence[Mapping[str, Any]]) -> list[str] | None:
    """The config tip for docs-only run-alls, or None when it does not apply.

    Fires only when every global finding is R001 and every R001 subject looks
    like documentation. The snippet lists exactly the patterns those subjects
    matched, nothing broader.
    """
    global_rules = [str(f.get("rule", "")) for f in findings if str(f.get("scope", "")) == "global"]
    if not global_rules or any(rule != "R001" for rule in global_rules):
        return None
    patterns: list[str] = []
    for finding in findings:
        if str(finding.get("rule", "")) != "R001":
            continue
        pattern = _docs_pattern_for(str(finding.get("subject", "")))
        if pattern is None:
            return None
        if pattern not in patterns:
            patterns.append(pattern)
    if not patterns:
        return None
    listed = ", ".join(f'"{pattern}"' for pattern in sorted(patterns))
    return [
        "> [!TIP]",
        "> Every reason above points at documentation-style files. If no test reads them,",
        "> you can tell acquit to treat them as inert:",
        ">",
        "> ```toml",
        "> # pyproject.toml",
        "> [tool.acquit]",
        f"> assume_inert = [{listed}]",
        "> ```",
        ">",
        "> Adding this means you vouch that no test reads these files;"
        " acquit takes your word for it.",
    ]


def _finding_key(finding: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(finding.get("rule", "")),
        str(finding.get("scope", "")),
        str(finding.get("subject", "")),
        str(finding.get("reason", "")),
    )


def _finding_table(findings: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["| Rule | Subject | Reason |", "| --- | --- | --- |"]
    for finding in findings:
        rule = _cell(str(finding.get("rule", "?")))
        subject = _cell(str(finding.get("subject", "?")))
        reason = _cell(str(finding.get("reason", "?")))
        lines.append(f"| `{rule}` | `{subject}` | {reason} |")
    return lines


def _render_run_all(
    blockers: Sequence[Mapping[str, Any]], observations: Sequence[Mapping[str, Any]]
) -> list[str]:
    lines = ["## Acquit: ran everything", ""]
    if not blockers:
        prefix = "No rule fired" if not observations else "No finding forced the full suite"
        lines.append(
            f"{prefix}; the change reaches every test file through the import graph, "
            "so nothing could be skipped."
        )
    else:
        lines += [
            "No test file could be proven unaffected, so the full suite ran. The blockers:",
            "",
            *_finding_table(blockers),
        ]
    if observations:
        plural = "finding" if len(observations) == 1 else "findings"
        lines += [
            "",
            "<details>",
            f"<summary>{len(observations)} non-blocking {plural}</summary>",
            "",
            "These findings were observed but did not force the full suite.",
            "",
            *_finding_table(observations),
            "",
            "</details>",
        ]
    nudge = _docs_nudge(blockers)
    if nudge is not None:
        lines += ["", *nudge]
    return lines


def render_comment(report: Mapping[str, Any]) -> str:
    """Render one report document as the sticky comment body. Pure."""
    digest = digest_report(report)
    decision = report.get("decision")
    findings_raw = decision.get("findings") if isinstance(decision, Mapping) else None
    findings = [f for f in findings_raw if isinstance(f, Mapping)] if findings_raw else []
    blockers_raw = decision.get("blockers") if isinstance(decision, Mapping) else None
    if isinstance(blockers_raw, list):
        blockers = [finding for finding in blockers_raw if isinstance(finding, Mapping)]
        blocker_keys = {_finding_key(finding) for finding in blockers}
        observations = [
            finding for finding in findings if _finding_key(finding) not in blocker_keys
        ]
    else:
        # Reports predating blocker accounting treated every finding as a
        # reason. Preserve that rendering when reading an old document.
        blockers = findings
        observations = []
    tests_raw = report.get("tests")
    tests: Mapping[str, Any] = tests_raw if isinstance(tests_raw, Mapping) else {}
    if digest.mode == str(SelectionMode.SELECTIVE):
        body = _render_selective(digest, tests)
    else:
        body = _render_run_all(blockers, observations)
    return "\n".join([COMMENT_MARKER, "", *body]) + "\n"


class Opener(Protocol):
    """One HTTP request in, the response body out.

    urlopen semantics: any non-2xx status raises. Tests inject a fake; the
    production opener below is the only code that ever touches the network.
    """

    def open(self, request: urllib.request.Request, *, timeout: float) -> bytes: ...


class UrllibOpener:
    """The production transport: stdlib urllib, an explicit timeout, nothing else."""

    def open(self, request: urllib.request.Request, *, timeout: float) -> bytes:
        # GITHUB_API_URL comes from the environment; refuse anything that is
        # not https so a hostile value cannot redirect the token to file: or
        # custom schemes.
        if not request.full_url.startswith("https://"):
            raise ValueError(f"refusing non-https API url: {request.full_url!r}")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: bytes = response.read()
        return body


@dataclass(frozen=True, slots=True)
class CommentTarget:
    api_url: str
    repository: str
    pr_number: int
    token: str


def _pr_from_ref(ref: str) -> int | None:
    match = _PR_REF_PATTERN.fullmatch(ref.strip())
    return int(match.group(1)) if match else None


def resolve_target(env: Mapping[str, str], pr_number: int | None) -> CommentTarget:
    """Assemble the API coordinates from the Actions environment."""
    token = env.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise AcquitError("GITHUB_TOKEN is not set")
    repository = env.get("GITHUB_REPOSITORY", "").strip()
    if not _REPO_PATTERN.fullmatch(repository):
        raise AcquitError("GITHUB_REPOSITORY is not set to owner/repo")
    api_url = env.get("GITHUB_API_URL", "").strip() or _DEFAULT_API_URL
    number = pr_number if pr_number is not None else _pr_from_ref(env.get("GITHUB_REF", ""))
    if number is None or number < 1:
        raise AcquitError("no pull request number: pass --pr or run on a pull_request event")
    return CommentTarget(
        api_url=api_url.rstrip("/"), repository=repository, pr_number=number, token=token
    )


def _request(
    target: CommentTarget, method: str, path: str, payload: Mapping[str, Any] | None
) -> urllib.request.Request:
    headers = {
        "Authorization": f"Bearer {target.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": f"acquit/{__version__}",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    url = f"{target.api_url}/repos/{target.repository}/{path}"
    return urllib.request.Request(url, data=data, headers=headers, method=method)


def _send(opener: Opener, request: urllib.request.Request) -> bytes:
    # CI networks flake; one retry, then give up. A comment is not worth more.
    try:
        return opener.open(request, timeout=_TIMEOUT_SECONDS)
    except Exception:
        return opener.open(request, timeout=_TIMEOUT_SECONDS)


def _find_existing_comment(target: CommentTarget, opener: Opener) -> int | None:
    """The id of the issue comment carrying the marker, or None."""
    for page in range(1, _MAX_PAGES + 1):
        path = f"issues/{target.pr_number}/comments?per_page={_PER_PAGE}&page={page}"
        listing: Any = json.loads(_send(opener, _request(target, "GET", path, None)))
        if not isinstance(listing, list):
            raise AcquitError("comment listing is not a json array")
        for entry in listing:
            if not isinstance(entry, dict):
                continue
            identifier = entry.get("id")
            if COMMENT_MARKER in str(entry.get("body", "")) and isinstance(identifier, int):
                return identifier
        if len(listing) < _PER_PAGE:
            return None
    return None


def upsert_comment(body: str, target: CommentTarget, opener: Opener) -> str:
    """Create or update the marker comment; returns "created" or "updated"."""
    existing = _find_existing_comment(target, opener)
    if existing is None:
        path = f"issues/{target.pr_number}/comments"
        _send(opener, _request(target, "POST", path, {"body": body}))
        return "created"
    _send(opener, _request(target, "PATCH", f"issues/comments/{existing}", {"body": body}))
    return "updated"


def run_comment(
    report_path: Path,
    pr_number: int | None,
    env: Mapping[str, str] | None = None,
    opener: Opener | None = None,
) -> int:
    """The `acquit comment` command body. Never fails: a lost comment is a warning."""
    try:
        document: Any = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise AcquitError(f"{report_path}: report must be a json object")
        body = render_comment(document)
        target = resolve_target(os.environ if env is None else env, pr_number)
        outcome = upsert_comment(body, target, UrllibOpener() if opener is None else opener)
        print(f"acquit: comment {outcome} on PR #{target.pr_number}")
    except Exception as error:
        print(f"acquit: warning: PR comment skipped: {error}", file=sys.stderr)
    return ExitCode.OK
