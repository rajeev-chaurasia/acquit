import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ScenarioRepo

from acquit.cli import main
from acquit.constants import ENV_SELECTION_FILE
from acquit.errors import ExitCode
from acquit.report import to_canonical_json

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")


def _select_args(repo: ScenarioRepo, out: Path, base: str, head: str | None) -> list[str]:
    args = [
        "select",
        "--base",
        base,
        "--report",
        str(out / "report.json"),
        "--selection",
        str(out / "selection.json"),
        "--witnesses",
        str(out / "witnesses.json"),
    ]
    if head is not None:
        args += ["--head", head]
    return args


def _read(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_select_writes_report_selection_and_witnesses(
    scenario_repo: ScenarioRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(
        _select_args(scenario_repo, tmp_path, scenario_repo.base, scenario_repo.alpha_change)
    )

    assert exit_code == ExitCode.OK
    out = capsys.readouterr().out.strip()
    assert out == "acquit: selective: 1 selected, 0 always-run, 3 skipped of 4 tests"

    report = _read(tmp_path / "report.json")
    selection = _read(tmp_path / "selection.json")
    witnesses = _read(tmp_path / "witnesses.json")
    assert report["decision"]["mode"] == "selective"
    assert report["run"]["base_sha"] == scenario_repo.base
    assert report["run"]["head_sha"] == scenario_repo.alpha_change
    assert report["changed"] == [{"path": "alpha.py", "kind": "module", "status": "modified"}]
    assert report["graph"]["hash"] == selection["graph_hash"] == witnesses["graph_hash"]
    assert report["stats"]["estimated_seconds_saved"] is None
    assert selection["mode"] == "selective"
    assert selection["skip"] == [
        "tests/pkg/test_pkg.py",
        "tests/test_beta.py",
        "tests/test_delta.py",
    ]
    assert [entry["test"] for entry in witnesses["witnesses"]] == selection["skip"]


def test_select_run_all_from_rules_exits_ok(
    scenario_repo: ScenarioRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(
        _select_args(
            scenario_repo, tmp_path, scenario_repo.conftest_change, scenario_repo.manifest_change
        )
    )

    assert exit_code == ExitCode.OK
    assert capsys.readouterr().out.strip() == "acquit: run-all: R002:pyproject.toml (4 tests)"
    report = _read(tmp_path / "report.json")
    selection = _read(tmp_path / "selection.json")
    assert report["decision"]["mode"] == "run-all"
    assert [f["rule"] for f in report["decision"]["findings"]] == ["R002"]
    assert selection["mode"] == "run-all"
    assert selection["skip"] == []


def test_select_durations_estimate_seconds_saved(
    scenario_repo: ScenarioRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(scenario_repo.path)
    durations = tmp_path / "durations.json"
    durations.write_text(
        json.dumps(
            {
                "tests/test_alpha.py": 9.9,
                "tests/test_beta.py": 3.5,
                "tests/test_delta.py": 1.5,
                "tests/pkg/test_pkg.py": 0.25,
            }
        ),
        encoding="utf-8",
    )

    args = _select_args(scenario_repo, tmp_path, scenario_repo.base, scenario_repo.alpha_change)
    exit_code = main([*args, "--durations", str(durations)])

    assert exit_code == ExitCode.OK
    stats = _read(tmp_path / "report.json")["stats"]
    assert stats["estimated_seconds_saved"] == 5.25
    assert stats["durations_source"] == "durations-file"


def test_select_output_is_deterministic(
    scenario_repo: ScenarioRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(scenario_repo.path)
    first, second = tmp_path / "one", tmp_path / "two"
    for out in (first, second):
        out.mkdir()
        exit_code = main(
            _select_args(scenario_repo, out, scenario_repo.base, scenario_repo.alpha_change)
        )
        assert exit_code == ExitCode.OK

    for name in ("selection.json", "witnesses.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    report_one = _read(first / "report.json")
    report_two = _read(second / "report.json")
    report_one["run"]["created_at"] = report_two["run"]["created_at"]
    assert to_canonical_json(report_one) == to_canonical_json(report_two)


def test_selection_file_drives_pytest_deselection(
    scenario_repo: ScenarioRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(scenario_repo.path)
    exit_code = main(
        _select_args(scenario_repo, tmp_path, scenario_repo.base, scenario_repo.alpha_change)
    )
    assert exit_code == ExitCode.OK

    env = dict(os.environ)
    env[ENV_SELECTION_FILE] = str(tmp_path / "selection.json")
    env["PYTHONPATH"] = str(scenario_repo.path)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=scenario_repo.path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout
    assert "3 deselected" in completed.stdout


def test_internal_failure_writes_run_all_docs_and_exits_internal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "not-a-repo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    report_path = tmp_path / "report.json"
    selection_path = tmp_path / "selection.json"

    exit_code = main(
        [
            "select",
            "--base",
            "main",
            "--report",
            str(report_path),
            "--selection",
            str(selection_path),
            "--witnesses",
            str(tmp_path / "witnesses.json"),
        ]
    )

    assert exit_code == ExitCode.INTERNAL
    assert "internal error" in capsys.readouterr().err
    report = _read(report_path)
    assert report["decision"]["mode"] == "run-all"
    (finding,) = report["decision"]["findings"]
    assert finding["rule"] == "R018"
    assert finding["reason"]
    selection = _read(selection_path)
    assert selection["mode"] == "run-all"
    assert selection["skip"] == []


def test_analyze_prints_graph_health(
    scenario_repo: ScenarioRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(["analyze"])

    assert exit_code == ExitCode.OK
    health = json.loads(capsys.readouterr().out)
    assert health["files"] == 10
    assert health["nodes"] == 10
    assert health["edges"] == 4
    assert health["tests"] == 4
    assert health["tainted"] == 0
    # "" is detected; the test basedirs are pytest's runtime sys.path inserts.
    assert health["roots"] == ["", "tests", "tests/pkg"]
    assert len(health["graph_hash"]) == 64


def test_version_flag_exits_zero() -> None:
    try:
        main(["--version"])
    except SystemExit as error:
        assert error.code == 0
