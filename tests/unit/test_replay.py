"""Replay round trips: witnesses must be machine-checkable, not logs."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import ScenarioRepo

from acquit.cli import main
from acquit.errors import ExitCode

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")


@pytest.fixture(scope="module")
def select_docs(
    scenario_repo: ScenarioRepo, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, Path]:
    out = tmp_path_factory.mktemp("replay-docs")
    report = out / "report.json"
    witnesses = out / "witnesses.json"
    with pytest.MonkeyPatch.context() as patcher:
        patcher.chdir(scenario_repo.path)
        exit_code = main(
            [
                "select",
                "--base",
                scenario_repo.base,
                "--head",
                scenario_repo.alpha_change,
                "--report",
                str(report),
                "--selection",
                str(out / "selection.json"),
                "--witnesses",
                str(witnesses),
            ]
        )
    assert exit_code == ExitCode.OK
    return report, witnesses


def _load(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _dump(document: dict[str, Any], path: Path) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_replay_verifies_every_witness(
    select_docs: tuple[Path, Path],
    scenario_repo: ScenarioRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report, witnesses = select_docs
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(["replay", str(report), "--witnesses", str(witnesses)])

    assert exit_code == ExitCode.OK
    assert capsys.readouterr().out.strip() == "replay ok: 3 witnesses verified"


def test_replay_detects_a_tampered_closure_hash(
    select_docs: tuple[Path, Path],
    scenario_repo: ScenarioRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report, witnesses = select_docs
    document = _load(witnesses)
    document["witnesses"][0]["closure"] = "0" * 64
    tampered = _dump(document, tmp_path / "witnesses.json")
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(["replay", str(report), "--witnesses", str(tampered)])

    assert exit_code == ExitCode.REPLAY_MISMATCH
    assert "closure hash mismatch" in capsys.readouterr().err


def test_replay_detects_a_tampered_graph_hash(
    select_docs: tuple[Path, Path],
    scenario_repo: ScenarioRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report, witnesses = select_docs
    document = _load(report)
    document["graph"]["hash"] = "f" * 64
    tampered = _dump(document, tmp_path / "report.json")
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(["replay", str(tampered), "--witnesses", str(witnesses)])

    assert exit_code == ExitCode.REPLAY_MISMATCH
    assert "graph hash mismatch" in capsys.readouterr().err


def test_replay_refuses_working_tree_reports(
    select_docs: tuple[Path, Path],
    scenario_repo: ScenarioRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report, witnesses = select_docs
    document = _load(report)
    document["run"]["head_sha"] = None
    working_tree = _dump(document, tmp_path / "report.json")
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(["replay", str(working_tree), "--witnesses", str(witnesses)])

    assert exit_code == ExitCode.USAGE
    assert "replay needs a commit" in capsys.readouterr().err


def test_replay_missing_witnesses_file_is_a_usage_error(
    select_docs: tuple[Path, Path],
    scenario_repo: ScenarioRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _ = select_docs
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(["replay", str(report), "--witnesses", str(tmp_path / "missing.json")])

    assert exit_code == ExitCode.USAGE
