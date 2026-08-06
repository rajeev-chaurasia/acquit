import json
from pathlib import Path

from acquit.cli import main
from acquit.errors import ExitCode


def test_select_emits_run_all_documents(tmp_path: Path) -> None:
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
        ]
    )

    assert exit_code == ExitCode.OK
    report = json.loads(report_path.read_text(encoding="utf-8"))
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    assert report["decision"]["mode"] == "run-all"
    assert selection["mode"] == "run-all"
    assert selection["skip"] == []


def test_version_flag_exits_zero() -> None:
    try:
        main(["--version"])
    except SystemExit as error:
        assert error.code == 0
