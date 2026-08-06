"""Manifest round-trips, shard slicing, and sampling against a fake API."""

import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from acquit.errors import AcquitError
from acquit.study.manifest import (
    Exclusion,
    Manifest,
    PrRecord,
    load_manifest,
    manifest_from_dict,
    manifest_to_dict,
    months_before,
    parse_shard,
    sample_pulls,
    shard_slice,
    with_exclusion,
    write_manifest,
)

API = "https://api.test"


def _record(number: int) -> PrRecord:
    return PrRecord(
        number=number,
        base_sha=f"base{number:03d}",
        head_sha=f"head{number:03d}",
        merge_sha=f"merge{number:03d}",
        title=f"pr {number}",
    )


def _manifest() -> Manifest:
    return Manifest(
        repo_url="https://github.com/pallets/flask.git",
        repo_slug="pallets/flask",
        python_version="3.12",
        constraints_sha256="ab" * 32,
        window_months=18,
        prs=tuple(_record(number) for number in (30, 26, 12)),
        excluded=(Exclusion(number=12, reason="base-suite: pytest exited with 2"),),
    )


def test_manifest_round_trip(tmp_path: Path) -> None:
    manifest = _manifest()
    path = tmp_path / "flask.json"
    write_manifest(path, manifest)
    assert load_manifest(path) == manifest


def test_round_trip_preserves_pr_order() -> None:
    manifest = _manifest()
    rebuilt = manifest_from_dict(json.loads(json.dumps(manifest_to_dict(manifest))))
    assert [pr.number for pr in rebuilt.prs] == [30, 26, 12]


def test_manifest_rejects_wrong_schema() -> None:
    data = manifest_to_dict(_manifest())
    data["schema"] = "acquit/other-v1"
    with pytest.raises(AcquitError):
        manifest_from_dict(data)


def test_manifest_rejects_malformed_pr() -> None:
    data = manifest_to_dict(_manifest())
    data["prs"][0]["base_sha"] = 5
    with pytest.raises(AcquitError):
        manifest_from_dict(data)


def test_with_exclusion_appends_and_dedupes() -> None:
    manifest = _manifest()
    grown = with_exclusion(manifest, 26, "fetch: gone")
    assert [entry.number for entry in grown.excluded] == [12, 26]
    again = with_exclusion(grown, 26, "another reason")
    assert again == grown


def test_parse_shard() -> None:
    assert parse_shard("3/20") == (3, 20)
    assert parse_shard("1/1") == (1, 1)
    for bad in ("0/5", "6/5", "x/5", "3", "3/", "/5", "-1/5"):
        with pytest.raises(AcquitError):
            parse_shard(bad)


def test_shard_slices_partition_deterministically() -> None:
    prs = tuple(_record(number) for number in range(1, 11))
    shards = [shard_slice(prs, index, 3) for index in (1, 2, 3)]
    assert shards == [shard_slice(prs, index, 3) for index in (1, 2, 3)]
    seen = [pr for shard in shards for pr in shard]
    assert sorted(pr.number for pr in seen) == list(range(1, 11))
    assert len(seen) == len(set(pr.number for pr in seen))
    # Relative order within a shard follows the manifest order.
    assert [pr.number for pr in shards[0]] == [1, 4, 7, 10]


def test_shard_slice_rejects_out_of_range() -> None:
    prs = (_record(1),)
    with pytest.raises(AcquitError):
        shard_slice(prs, 2, 1)


def test_months_before_clamps_month_end() -> None:
    moment = datetime(2026, 3, 31, tzinfo=UTC)
    assert months_before(moment, 1) == datetime(2026, 2, 28, tzinfo=UTC)
    assert months_before(datetime(2026, 1, 15, tzinfo=UTC), 18) == datetime(2024, 7, 15, tzinfo=UTC)


class FakeOpener:
    """Maps full URLs to json payloads and records every request made."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, *, timeout: float) -> bytes:
        self.requests.append(request)
        return json.dumps(self.responses[request.full_url]).encode("utf-8")


def _list_url(page: int) -> str:
    return (
        f"{API}/repos/o/r/pulls?state=closed&sort=created&direction=desc&per_page=100&page={page}"
    )


def _pull(number: int, merged_at: str | None, created_at: str) -> dict[str, object]:
    return {
        "number": number,
        "merged_at": merged_at,
        "created_at": created_at,
        "merge_commit_sha": f"merge{number:03d}",
        "title": f"pr {number}",
        "base": {"sha": f"base{number:03d}"},
        "head": {"sha": f"head{number:03d}"},
    }


def _responses() -> dict[str, object]:
    return {
        _list_url(1): [
            _pull(30, "2026-06-01T00:00:00Z", "2026-05-20T00:00:00Z"),
            _pull(29, None, "2026-05-10T00:00:00Z"),
            _pull(28, "2026-05-01T00:00:00Z", "2026-04-20T00:00:00Z"),
            _pull(27, "2025-05-30T00:00:00Z", "2025-05-01T00:00:00Z"),
            _pull(26, "2026-04-01T00:00:00Z", "2026-03-20T00:00:00Z"),
        ],
        _list_url(2): [],
        f"{API}/repos/o/r/pulls/30": {"changed_files": 10},
        f"{API}/repos/o/r/pulls/28": {"changed_files": 3000},
        f"{API}/repos/o/r/pulls/26": {"changed_files": 5},
    }


def test_sample_filters_and_orders_newest_first() -> None:
    opener = FakeOpener(_responses())
    result = sample_pulls("o/r", 10, 12, opener, {}, api_url=API)
    # 29 never merged, 28 too large, 27 outside the 12-month window from 2026-06-01.
    assert [pr.number for pr in result.prs] == [30, 26]
    assert result.prs[0].base_sha == "base030"
    assert result.prs[0].head_sha == "head030"
    assert result.prs[0].merge_sha == "merge030"
    assert any("2000" in note and "28" in note for note in result.notes)


def test_sample_count_cap_stops_early() -> None:
    opener = FakeOpener(_responses())
    result = sample_pulls("o/r", 1, 12, opener, {}, api_url=API)
    assert [pr.number for pr in result.prs] == [30]
    fetched = {request.full_url for request in opener.requests}
    # The cap is reached before pr 28's detail is ever requested.
    assert f"{API}/repos/o/r/pulls/28" not in fetched


def test_sample_sends_token_when_present() -> None:
    opener = FakeOpener(_responses())
    sample_pulls("o/r", 10, 12, opener, {"GH_TOKEN": "sekret"}, api_url=API)
    assert all(
        request.get_header("Authorization") == "Bearer sekret" for request in opener.requests
    )


def test_sample_omits_auth_without_token() -> None:
    opener = FakeOpener(_responses())
    sample_pulls("o/r", 10, 12, opener, {}, api_url=API)
    assert all(request.get_header("Authorization") is None for request in opener.requests)


def test_sample_rejects_bad_slug() -> None:
    with pytest.raises(AcquitError):
        sample_pulls("not a slug", 10, 12, FakeOpener({}), {}, api_url=API)
