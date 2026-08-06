"""Study manifests: the committed record of which PRs the study replays.

Sampling talks to the GitHub REST API through the same injectable Opener the
PR comment code uses, so tests never touch the network. Every timestamp that
shapes a committed manifest comes from API payloads; nothing in this module
reads the local clock.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from acquit import __version__
from acquit.errors import AcquitError
from acquit.gh.comment import Opener
from acquit.report import to_canonical_json
from acquit.study import MANIFEST_SCHEMA

_API_VERSION: Final = "2022-11-28"
_DEFAULT_API_URL: Final = "https://api.github.com"
_PER_PAGE: Final = 100
# A looping or hostile API must not hold sampling hostage; stop paging here.
_MAX_PAGES: Final = 50
_TIMEOUT_SECONDS: Final = 30.0

# PRs touching more files than this are bulk refactors the study cannot learn from.
MAX_CHANGED_FILES: Final = 2000

_SLUG_PATTERN: Final = re.compile(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")


@dataclass(frozen=True, slots=True)
class PrRecord:
    """One merged pull request the study will replay."""

    number: int
    base_sha: str
    head_sha: str
    merge_sha: str | None
    title: str


@dataclass(frozen=True, slots=True)
class Exclusion:
    """A PR the runner could not replay, with the reason it was dropped."""

    number: int
    reason: str


@dataclass(frozen=True, slots=True)
class Manifest:
    """Everything needed to re-run the study from a committed file."""

    repo_url: str
    repo_slug: str
    python_version: str
    constraints_sha256: str | None
    window_months: int
    prs: tuple[PrRecord, ...]
    excluded: tuple[Exclusion, ...]


def sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_to_dict(manifest: Manifest) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "repo_url": manifest.repo_url,
        "repo_slug": manifest.repo_slug,
        "python_version": manifest.python_version,
        "constraints_sha256": manifest.constraints_sha256,
        "window_months": manifest.window_months,
        "prs": [
            {
                "number": pr.number,
                "base_sha": pr.base_sha,
                "head_sha": pr.head_sha,
                "merge_sha": pr.merge_sha,
                "title": pr.title,
            }
            for pr in manifest.prs
        ],
        "excluded": [
            {"number": entry.number, "reason": entry.reason} for entry in manifest.excluded
        ],
    }


def _field_str(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str):
        raise AcquitError(f"{where}: {key} must be a string")
    return value


def _field_int(entry: Mapping[str, Any], key: str, where: str) -> int:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AcquitError(f"{where}: {key} must be an integer")
    return value


def manifest_from_dict(data: Mapping[str, Any]) -> Manifest:
    if data.get("schema") != MANIFEST_SCHEMA:
        raise AcquitError(f"not a {MANIFEST_SCHEMA} document")
    prs_raw = data.get("prs")
    if not isinstance(prs_raw, list):
        raise AcquitError("manifest: prs must be a list")
    prs: list[PrRecord] = []
    for entry in prs_raw:
        if not isinstance(entry, Mapping):
            raise AcquitError("manifest: every prs entry must be an object")
        where = f"pr {entry.get('number')!r}"
        merge_raw = entry.get("merge_sha")
        prs.append(
            PrRecord(
                number=_field_int(entry, "number", where),
                base_sha=_field_str(entry, "base_sha", where),
                head_sha=_field_str(entry, "head_sha", where),
                merge_sha=merge_raw if isinstance(merge_raw, str) else None,
                title=_field_str(entry, "title", where),
            )
        )
    excluded_raw = data.get("excluded")
    if not isinstance(excluded_raw, list):
        raise AcquitError("manifest: excluded must be a list")
    excluded: list[Exclusion] = []
    for entry in excluded_raw:
        if not isinstance(entry, Mapping):
            raise AcquitError("manifest: every excluded entry must be an object")
        where = f"exclusion {entry.get('number')!r}"
        excluded.append(
            Exclusion(
                number=_field_int(entry, "number", where),
                reason=_field_str(entry, "reason", where),
            )
        )
    constraints_raw = data.get("constraints_sha256")
    return Manifest(
        repo_url=_field_str(data, "repo_url", "manifest"),
        repo_slug=_field_str(data, "repo_slug", "manifest"),
        python_version=_field_str(data, "python_version", "manifest"),
        constraints_sha256=constraints_raw if isinstance(constraints_raw, str) else None,
        window_months=_field_int(data, "window_months", "manifest"),
        prs=tuple(prs),
        excluded=tuple(excluded),
    )


def load_manifest(path: Path) -> Manifest:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AcquitError(f"could not read manifest {path}: {error}") from error
    if not isinstance(data, dict):
        raise AcquitError(f"{path}: manifest must be a json object")
    return manifest_from_dict(data)


def write_manifest(path: Path, manifest: Manifest) -> None:
    path.write_text(to_canonical_json(manifest_to_dict(manifest)), encoding="utf-8")


def with_exclusion(manifest: Manifest, number: int, reason: str) -> Manifest:
    """Append one exclusion; a PR already excluded keeps its original reason."""
    if any(entry.number == number for entry in manifest.excluded):
        return manifest
    return replace(manifest, excluded=(*manifest.excluded, Exclusion(number, reason)))


def parse_shard(spec: str) -> tuple[int, int]:
    """Parse a K/N shard spec into (index, count), both one-based."""
    index_raw, sep, count_raw = spec.partition("/")
    if not sep or not index_raw.isdigit() or not count_raw.isdigit():
        raise AcquitError(f"shard must look like K/N, got {spec!r}")
    index, count = int(index_raw), int(count_raw)
    if count < 1 or not 1 <= index <= count:
        raise AcquitError(f"shard index out of range: {spec!r}")
    return index, count


def shard_slice(prs: tuple[PrRecord, ...], index: int, count: int) -> tuple[PrRecord, ...]:
    """Deterministic round-robin split: shard K of N takes positions K-1, K-1+N, ...

    Round-robin keeps every shard's workload mixed across the sampling window
    instead of handing one shard all the oldest, slowest PRs.
    """
    if count < 1 or not 1 <= index <= count:
        raise AcquitError(f"shard {index}/{count} is out of range")
    return prs[index - 1 :: count]


@dataclass(frozen=True, slots=True)
class SampleResult:
    """What sampling produced: the records plus notes about anything skipped."""

    prs: tuple[PrRecord, ...]
    notes: tuple[str, ...]


def months_before(moment: datetime, months: int) -> datetime:
    """Calendar arithmetic: the same day N months earlier, clamped to month end."""
    total = moment.year * 12 + (moment.month - 1) - months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _auth_token(env: Mapping[str, str]) -> str | None:
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = env.get(key, "").strip()
        if value:
            return value
    return None


def _request(url: str, token: str | None) -> urllib.request.Request:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": f"acquit-study/{__version__}",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _get_json(opener: Opener, url: str, token: str | None) -> Any:
    return json.loads(opener.open(_request(url, token), timeout=_TIMEOUT_SECONDS))


def _api_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _list_url(api_url: str, slug: str, page: int) -> str:
    return (
        f"{api_url}/repos/{slug}/pulls"
        f"?state=closed&sort=created&direction=desc&per_page={_PER_PAGE}&page={page}"
    )


def _pull_record(
    entry: Mapping[str, Any],
    slug: str,
    opener: Opener,
    token: str | None,
    api_url: str,
    notes: list[str],
) -> PrRecord | None:
    number = entry.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        notes.append("pull without a usable number skipped")
        return None
    base, head = entry.get("base"), entry.get("head")
    base_sha = base.get("sha") if isinstance(base, Mapping) else None
    head_sha = head.get("sha") if isinstance(head, Mapping) else None
    if not isinstance(base_sha, str) or not isinstance(head_sha, str):
        notes.append(f"pr {number}: missing base or head sha, skipped")
        return None
    # changed_files only appears on the detail payload, one extra request per PR.
    detail = _get_json(opener, f"{api_url}/repos/{slug}/pulls/{number}", token)
    changed = detail.get("changed_files") if isinstance(detail, Mapping) else None
    if isinstance(changed, int) and changed > MAX_CHANGED_FILES:
        notes.append(f"pr {number}: {changed} changed files, over the {MAX_CHANGED_FILES} cap")
        return None
    merge_raw = entry.get("merge_commit_sha")
    title_raw = entry.get("title")
    return PrRecord(
        number=number,
        base_sha=base_sha,
        head_sha=head_sha,
        merge_sha=merge_raw if isinstance(merge_raw, str) else None,
        title=title_raw if isinstance(title_raw, str) else "",
    )


def sample_pulls(
    slug: str,
    count: int,
    window_months: int,
    opener: Opener,
    env: Mapping[str, str],
    api_url: str = _DEFAULT_API_URL,
) -> SampleResult:
    """List merged PRs newest-first from the GitHub REST API.

    The recency window anchors on the merged_at of the first merged PR the
    listing returns, never the local clock, so re-sampling against unchanged
    history is stable. PRs over the changed-file cap and PRs closed without
    merging are skipped with a note.
    """
    if not _SLUG_PATTERN.fullmatch(slug):
        raise AcquitError(f"repo must be owner/name, got {slug!r}")
    if count < 1:
        raise AcquitError("count must be at least 1")
    token = _auth_token(env)
    picked: list[tuple[datetime, PrRecord]] = []
    notes: list[str] = []
    cutoff: datetime | None = None
    for page in range(1, _MAX_PAGES + 1):
        listing = _get_json(opener, _list_url(api_url, slug, page), token)
        if not isinstance(listing, list):
            raise AcquitError("pull listing is not a json array")
        if not listing:
            break
        any_fresh = False
        for entry in listing:
            if not isinstance(entry, Mapping):
                continue
            merged_at = _api_time(entry.get("merged_at"))
            created_at = _api_time(entry.get("created_at"))
            if cutoff is None and merged_at is not None:
                cutoff = months_before(merged_at, window_months)
            if created_at is not None and (cutoff is None or created_at >= cutoff):
                any_fresh = True
            if merged_at is None or (cutoff is not None and merged_at < cutoff):
                continue
            record = _pull_record(entry, slug, opener, token, api_url, notes)
            if record is not None:
                picked.append((merged_at, record))
                if len(picked) >= count:
                    break
        # The listing is created-desc; a page with nothing inside the window
        # means everything further back is older still.
        if len(picked) >= count or not any_fresh:
            break
    picked.sort(key=lambda item: (item[0], item[1].number), reverse=True)
    return SampleResult(prs=tuple(record for _, record in picked), notes=tuple(notes))
