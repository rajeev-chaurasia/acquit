"""GITHUB_OUTPUT and GITHUB_STEP_SUMMARY writing, driven through tmp files."""

import json
from pathlib import Path
from typing import Any

import pytest

from acquit.errors import AcquitError, ExitCode
from acquit.gh.outputs import DELIMITER, format_output_line, run_ci_outputs

SELECTIVE_REPORT = {
    "schema": "acquit/report-v1",
    "decision": {"mode": "selective", "findings": []},
    "stats": {
        "selected": 1,
        "skipped": 3,
        "always_run": 0,
        "total": 4,
        "estimated_seconds_saved": None,
    },
}
SELECTIVE_SELECTION = {"schema": "acquit/selection-v2", "mode": "selective", "skip": []}
RUN_ALL_SELECTION = {"schema": "acquit/selection-v2", "mode": "run-all", "skip": []}
# The document the action's bash fallback writes when the tool itself failed.
FALLBACK_REPORT = {"schema": "acquit/report-v1", "decision": {"mode": "run-all"}}


def _entries(path: Path) -> dict[str, str]:
    """Parse heredoc-form records the way the Actions runner does."""
    pairs: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        key, sep, delimiter = lines[index].partition("<<")
        if sep and delimiter:
            value: list[str] = []
            index += 1
            while index < len(lines) and lines[index] != delimiter:
                value.append(lines[index])
                index += 1
            pairs[key] = "\n".join(value)
        index += 1
    return pairs


def _write_json(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def runner_files(tmp_path: Path) -> dict[str, str]:
    output = tmp_path / "outputs.txt"
    summary = tmp_path / "summary.md"
    output.write_text("", encoding="utf-8")
    summary.write_text("", encoding="utf-8")
    return {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}


def test_format_output_line_matches_the_action_heredoc_form() -> None:
    line = format_output_line("mode", "run-all")

    assert line == f"mode<<{DELIMITER}\nrun-all\n{DELIMITER}\n"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("mode", "a\nb"),
        ("mode", "a\rb"),
        ("mode", f"x{DELIMITER}y"),
        ("bad name", "value"),
        ("", "value"),
        ("1mode", "value"),
    ],
)
def test_format_output_line_refuses_hostile_records(name: str, value: str) -> None:
    with pytest.raises(AcquitError, match="refusing"):
        format_output_line(name, value)


def test_ci_outputs_writes_mode_counts_and_paths(
    tmp_path: Path, runner_files: dict[str, str]
) -> None:
    report = _write_json(tmp_path / "report.json", SELECTIVE_REPORT)
    selection = _write_json(tmp_path / "selection.json", SELECTIVE_SELECTION)

    exit_code = run_ci_outputs(report, selection, env=runner_files)

    assert exit_code == ExitCode.OK
    assert _entries(Path(runner_files["GITHUB_OUTPUT"])) == {
        "mode": "selective",
        "selected": "1",
        "skipped": "3",
        "always-run": "0",
        "total": "4",
        "report": str(report),
        "selection": str(selection),
    }
    summary = Path(runner_files["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8")
    assert "3 of 4 test files provably unaffected" in summary
    assert "`selective`" in summary


def test_ci_outputs_selection_mode_wins_over_the_report(
    tmp_path: Path, runner_files: dict[str, str]
) -> None:
    # Replay may rewrite the selection to run-all after the report was written;
    # the document pytest obeys is the one the outputs must describe.
    report = _write_json(tmp_path / "report.json", SELECTIVE_REPORT)
    selection = _write_json(tmp_path / "selection.json", RUN_ALL_SELECTION)

    assert run_ci_outputs(report, selection, env=runner_files) == ExitCode.OK

    assert _entries(Path(runner_files["GITHUB_OUTPUT"]))["mode"] == "run-all"
    summary = Path(runner_files["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8")
    assert "Every test file runs." in summary


def test_ci_outputs_handles_the_minimal_fallback_report(
    tmp_path: Path, runner_files: dict[str, str]
) -> None:
    report = _write_json(tmp_path / "report.json", FALLBACK_REPORT)
    selection = _write_json(tmp_path / "selection.json", RUN_ALL_SELECTION)

    assert run_ci_outputs(report, selection, env=runner_files) == ExitCode.OK

    entries = _entries(Path(runner_files["GITHUB_OUTPUT"]))
    assert entries["mode"] == "run-all"
    assert entries["skipped"] == "0"
    assert entries["total"] == "0"


def test_ci_outputs_appends_after_existing_records(
    tmp_path: Path, runner_files: dict[str, str]
) -> None:
    output = Path(runner_files["GITHUB_OUTPUT"])
    output.write_text(f"prior<<{DELIMITER}\nkept\n{DELIMITER}\n", encoding="utf-8")
    report = _write_json(tmp_path / "report.json", FALLBACK_REPORT)
    selection = _write_json(tmp_path / "selection.json", RUN_ALL_SELECTION)

    assert run_ci_outputs(report, selection, env=runner_files) == ExitCode.OK

    entries = _entries(output)
    assert entries["prior"] == "kept"
    assert entries["mode"] == "run-all"


def test_ci_outputs_refuses_a_newline_path_and_writes_nothing(
    tmp_path: Path, runner_files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write_json(tmp_path / "report.json", FALLBACK_REPORT)
    hostile = Path(str(tmp_path / "selection.json") + "\nmode=selective")

    assert run_ci_outputs(report, hostile, env=runner_files) == ExitCode.OK

    assert Path(runner_files["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == ""
    assert Path(runner_files["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8") == ""
    assert "warning: ci outputs skipped" in capsys.readouterr().err


def test_ci_outputs_without_github_output_warns_and_exits_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write_json(tmp_path / "report.json", FALLBACK_REPORT)
    selection = _write_json(tmp_path / "selection.json", RUN_ALL_SELECTION)

    assert run_ci_outputs(report, selection, env={}) == ExitCode.OK

    assert "GITHUB_OUTPUT" in capsys.readouterr().err


def test_ci_outputs_with_a_missing_selection_warns_and_exits_ok(
    tmp_path: Path, runner_files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write_json(tmp_path / "report.json", FALLBACK_REPORT)

    exit_code = run_ci_outputs(report, tmp_path / "absent.json", env=runner_files)

    assert exit_code == ExitCode.OK
    assert Path(runner_files["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == ""
    assert "warning: ci outputs skipped" in capsys.readouterr().err


def test_ci_outputs_skips_the_summary_when_env_is_absent(
    tmp_path: Path, runner_files: dict[str, str]
) -> None:
    report = _write_json(tmp_path / "report.json", FALLBACK_REPORT)
    selection = _write_json(tmp_path / "selection.json", RUN_ALL_SELECTION)
    env = {"GITHUB_OUTPUT": runner_files["GITHUB_OUTPUT"]}

    assert run_ci_outputs(report, selection, env=env) == ExitCode.OK

    assert _entries(Path(runner_files["GITHUB_OUTPUT"]))["mode"] == "run-all"
