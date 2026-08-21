"""Rendering and transport for the sticky PR comment. No test touches the network."""

import json
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest

from acquit.constants import COMMENT_MARKER
from acquit.errors import AcquitError, ExitCode
from acquit.gh.comment import render_comment, resolve_target, run_comment, upsert_comment

ENV = {
    "GITHUB_TOKEN": "token-123",
    "GITHUB_REPOSITORY": "octo/widgets",
    "GITHUB_REF": "refs/pull/7/merge",
}


def _selective_report(
    skipped: list[dict[str, str]],
    selected: list[dict[str, Any]] | None = None,
    always_run: list[dict[str, str]] | None = None,
    saved: float | None = None,
) -> dict[str, Any]:
    chosen = selected or []
    forced = always_run or []
    return {
        "schema": "acquit/report-v1",
        "decision": {"mode": "selective", "findings": [], "waivers": []},
        "tests": {"selected": chosen, "skipped": skipped, "always_run": forced},
        "stats": {
            "selected": len(chosen),
            "skipped": len(skipped),
            "always_run": len(forced),
            "total": len(chosen) + len(skipped) + len(forced),
            "estimated_seconds_saved": saved,
            "durations_source": None if saved is None else "durations-file",
        },
    }


def _run_all_report(
    findings: list[dict[str, str]], blockers: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    report = {
        "schema": "acquit/report-v1",
        "decision": {"mode": "run-all", "findings": findings, "waivers": []},
        "tests": {"selected": [], "skipped": [], "always_run": []},
        "stats": {
            "selected": 0,
            "skipped": 0,
            "always_run": 0,
            "total": 0,
            "estimated_seconds_saved": None,
            "durations_source": None,
        },
    }
    if blockers is not None:
        report["decision"]["blockers"] = blockers
    return report


def _finding(
    rule: str, subject: str, scope: str = "global", reason: str = "changed"
) -> dict[str, str]:
    return {"rule": rule, "scope": scope, "subject": subject, "reason": reason}


def _skipped(count: int) -> list[dict[str, str]]:
    return [
        {"path": f"tests/test_{index:03d}.py", "witness": f"w-{index:06d}"}
        for index in range(1, count + 1)
    ]


class FakeOpener:
    """Scripted transport: records every request, replays canned responses."""

    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.requests: list[urllib.request.Request] = []
        self._responses = responses

    def open(self, request: urllib.request.Request, *, timeout: float) -> bytes:
        assert timeout == 10.0
        self.requests.append(request)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_selective_comment_headline_details_and_reasons() -> None:
    report = _selective_report(
        skipped=[
            {"path": "tests/pkg/test_pkg.py", "witness": "w-000001"},
            {"path": "tests/test_beta.py", "witness": "w-000002"},
            {"path": "tests/test_delta.py", "witness": "w-000003"},
        ],
        selected=[{"path": "tests/test_alpha.py", "reasons": ["reachable-from:alpha.py"]}],
    )

    body = render_comment(report)

    assert body.startswith(COMMENT_MARKER + "\n")
    assert "## Acquit: 3 of 4 test files provably unaffected" in body
    assert "<details open>" in body
    assert "| `tests/test_beta.py` | `w-000002` |" in body
    assert "Still running: 1 selected, 0 always-run." in body
    assert "- Selected because: `reachable-from:alpha.py` (1)" in body
    assert "Estimated time saved" not in body


def test_selective_comment_reports_time_saved() -> None:
    body = render_comment(_selective_report(skipped=_skipped(2), saved=315.0))

    assert "Estimated time saved: about 5 minutes." in body

    body = render_comment(_selective_report(skipped=_skipped(2), saved=5.25))

    assert "Estimated time saved: about 5 seconds." in body


def test_selective_comment_names_always_run_findings() -> None:
    report = _selective_report(
        skipped=_skipped(1),
        always_run=[
            {"path": "tests/pkg/test_a.py", "finding": "R005:tests/pkg"},
            {"path": "tests/pkg/test_b.py", "finding": "R005:tests/pkg"},
        ],
    )

    body = render_comment(report)

    assert "Still running: 0 selected, 2 always-run." in body
    assert "- Always-run because: `R005:tests/pkg` (2)" in body


def test_selective_comment_collapses_a_long_skip_list() -> None:
    open_body = render_comment(_selective_report(skipped=_skipped(20)))
    folded_body = render_comment(_selective_report(skipped=_skipped(21)))

    assert "<details open>" in open_body
    assert "<details open>" not in folded_body
    assert "<details>" in folded_body
    # Folded, not dropped: every entry is still listed.
    assert "| `tests/test_021.py` | `w-000021` |" in folded_body


def test_run_all_comment_lists_findings() -> None:
    report = _run_all_report(
        [_finding("R002", "pyproject.toml", reason="changed dependency manifest")]
    )

    body = render_comment(report)

    assert body.startswith(COMMENT_MARKER + "\n")
    assert "## Acquit: ran everything" in body
    assert "| Rule | Subject | Reason |" in body
    assert "| `R002` | `pyproject.toml` | changed dependency manifest |" in body
    assert "[!TIP]" not in body


def test_run_all_comment_separates_blockers_from_non_blocking_findings() -> None:
    workflow = _finding("R003", ".github/workflows/ci.yml", reason="changed workflow")
    mutator = _finding(
        "R008",
        "backend/scripts/benchmark_27b.py",
        scope="global-if-reached",
        reason="mutates sys.path at import time",
    )
    report = _run_all_report([workflow, mutator], blockers=[workflow])

    body = render_comment(report)

    assert "The blockers:" in body
    assert "1 non-blocking finding" in body
    assert "These findings were observed but did not force the full suite." in body
    assert body.index("`.github/workflows/ci.yml`") < body.index("<details>")
    assert body.index("`backend/scripts/benchmark_27b.py`") > body.index("<details>")


def test_run_all_comment_without_findings_explains_full_impact() -> None:
    body = render_comment(_run_all_report([]))

    assert "## Acquit: ran everything" in body
    assert "No rule fired" in body
    assert "[!TIP]" not in body


def test_minimal_fallback_report_renders_as_run_all() -> None:
    # The document the action writes when the tool itself failed.
    body = render_comment({"schema": "acquit/report-v1", "decision": {"mode": "run-all"}})

    assert body.startswith(COMMENT_MARKER + "\n")
    assert "## Acquit: ran everything" in body


def test_docs_only_run_all_gets_the_config_nudge() -> None:
    report = _run_all_report([_finding("R001", "CHANGES.md"), _finding("R001", "docs/utils.md")])

    body = render_comment(report)

    snippet = (
        "> ```toml\n"
        "> # pyproject.toml\n"
        "> [tool.acquit]\n"
        '> assume_inert = ["CHANGES*", "docs/**"]\n'
        "> ```"
    )
    assert "[!TIP]" in body
    assert snippet in body
    assert "you vouch that no test reads these files" in body


def test_nudge_survives_scoped_findings_alongside_global_r001() -> None:
    report = _run_all_report(
        [
            _finding("R001", "docs/api.md"),
            _finding("R007", "examples/cli.py", scope="closure-taint"),
        ]
    )

    body = render_comment(report)

    assert "[!TIP]" in body
    assert 'assume_inert = ["docs/**"]' in body


def test_nudge_absent_when_a_global_finding_is_not_r001() -> None:
    report = _run_all_report([_finding("R001", "CHANGES.md"), _finding("R002", "pyproject.toml")])

    assert "[!TIP]" not in render_comment(report)


def test_nudge_absent_when_an_r001_subject_is_not_docs() -> None:
    report = _run_all_report([_finding("R001", "CHANGES.md"), _finding("R001", "data/fixture.csv")])

    assert "[!TIP]" not in render_comment(report)


def test_table_cells_cannot_break_the_markdown_table() -> None:
    report = _run_all_report([_finding("R001", "a|b.md", reason="pipe | and\nnewline")])

    body = render_comment(report)

    assert "| `a\\|b.md` | pipe \\| and newline |" in body


def test_resolve_target_reads_the_pull_request_ref() -> None:
    target = resolve_target(ENV, None)

    assert target.pr_number == 7
    assert target.api_url == "https://api.github.com"
    assert target.repository == "octo/widgets"


def test_resolve_target_prefers_the_explicit_pr_flag() -> None:
    assert resolve_target(ENV, 12).pr_number == 12


def test_resolve_target_honors_a_custom_api_url() -> None:
    env = dict(ENV, GITHUB_API_URL="https://ghe.example/api/v3/")

    assert resolve_target(env, None).api_url == "https://ghe.example/api/v3"


@pytest.mark.parametrize(
    "env",
    [
        {k: v for k, v in ENV.items() if k != "GITHUB_TOKEN"},
        dict(ENV, GITHUB_REPOSITORY="not-a-repo"),
        dict(ENV, GITHUB_REF="refs/heads/main"),
    ],
)
def test_resolve_target_rejects_an_incomplete_environment(env: dict[str, str]) -> None:
    with pytest.raises(AcquitError):
        resolve_target(env, None)


def test_upsert_creates_when_no_marker_comment_exists() -> None:
    target = resolve_target(ENV, None)
    opener = FakeOpener([b"[]", b"{}"])
    body = f"{COMMENT_MARKER}\n\nhello"

    assert upsert_comment(body, target, opener) == "created"

    listing, creation = opener.requests
    assert listing.get_method() == "GET"
    assert listing.full_url == (
        "https://api.github.com/repos/octo/widgets/issues/7/comments?per_page=100&page=1"
    )
    assert creation.get_method() == "POST"
    assert creation.full_url == "https://api.github.com/repos/octo/widgets/issues/7/comments"
    assert json.loads(creation.data or b"")["body"] == body
    assert creation.get_header("Authorization") == "Bearer token-123"


def test_upsert_updates_the_existing_marker_comment() -> None:
    target = resolve_target(ENV, None)
    listing = [
        {"id": 1, "body": "unrelated"},
        {"id": 42, "body": f"old {COMMENT_MARKER} body"},
    ]
    opener = FakeOpener([json.dumps(listing).encode("utf-8"), b"{}"])

    assert upsert_comment("new body", target, opener) == "updated"

    patch = opener.requests[-1]
    assert patch.get_method() == "PATCH"
    assert patch.full_url == "https://api.github.com/repos/octo/widgets/issues/comments/42"
    assert json.loads(patch.data or b"")["body"] == "new body"


def test_upsert_paginates_past_a_full_first_page() -> None:
    target = resolve_target(ENV, None)
    first = [{"id": index, "body": "chatter"} for index in range(100)]
    second = [{"id": 9, "body": COMMENT_MARKER}]
    opener = FakeOpener(
        [json.dumps(first).encode("utf-8"), json.dumps(second).encode("utf-8"), b"{}"]
    )

    assert upsert_comment("body", target, opener) == "updated"

    assert "page=1" in opener.requests[0].full_url
    assert "page=2" in opener.requests[1].full_url
    assert opener.requests[2].full_url.endswith("/issues/comments/9")


def _report_file(tmp_path: Path) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_run_all_report([])), encoding="utf-8")
    return path


def test_run_comment_posts_the_rendered_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report_file(tmp_path)
    opener = FakeOpener([b"[]", b"{}"])

    exit_code = run_comment(report, None, env=ENV, opener=opener)

    assert exit_code == ExitCode.OK
    posted = json.loads(opener.requests[-1].data or b"")["body"]
    assert posted == render_comment(_run_all_report([]))
    assert "comment created on PR #7" in capsys.readouterr().out


def test_run_comment_retries_a_flaky_request_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report_file(tmp_path)
    opener = FakeOpener([URLError("boom"), b"[]", b"{}"])

    assert run_comment(report, None, env=ENV, opener=opener) == ExitCode.OK

    assert len(opener.requests) == 3
    assert "comment created" in capsys.readouterr().out


def test_run_comment_gives_up_after_one_retry_and_exits_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report_file(tmp_path)
    opener = FakeOpener([URLError("down"), URLError("still down")])

    assert run_comment(report, None, env=ENV, opener=opener) == ExitCode.OK

    assert len(opener.requests) == 2
    assert "warning: PR comment skipped" in capsys.readouterr().err


def test_run_comment_without_a_token_warns_and_exits_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report_file(tmp_path)
    opener = FakeOpener([])
    env = {k: v for k, v in ENV.items() if k != "GITHUB_TOKEN"}

    assert run_comment(report, None, env=env, opener=opener) == ExitCode.OK

    assert opener.requests == []
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_run_comment_with_a_missing_report_warns_and_exits_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = FakeOpener([])

    exit_code = run_comment(tmp_path / "absent.json", None, env=ENV, opener=opener)

    assert exit_code == ExitCode.OK
    assert opener.requests == []
    assert "warning: PR comment skipped" in capsys.readouterr().err
