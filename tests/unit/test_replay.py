"""Replay round trips: witnesses must be machine-checkable, not logs."""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import ScenarioRepo, commit_all, init_repo, module_test_source, write_files

from acquit.cli import main
from acquit.config import load_config
from acquit.errors import ExitCode
from acquit.pipeline import snapshot_tree
from acquit.pytestmap.pytestcfg import load_pytest_config
from acquit.select import import_closure
from acquit.vcs import blob_shas
from acquit.witness import CLAIM_NARROWED, closure_hash

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


def test_replay_accepts_the_matching_selection_document(
    select_docs: tuple[Path, Path],
    scenario_repo: ScenarioRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, witnesses = select_docs
    selection = report.parent / "selection.json"
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(
        ["replay", str(report), "--witnesses", str(witnesses), "--selection", str(selection)]
    )

    assert exit_code == ExitCode.OK


def test_replay_rejects_a_selection_with_an_extra_skip(
    select_docs: tuple[Path, Path],
    scenario_repo: ScenarioRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report, witnesses = select_docs
    document = _load(report.parent / "selection.json")
    document["skip"].append({"path": "tests/test_alpha.py", "witness": "w-000099"})
    tampered = _dump(document, tmp_path / "selection.json")
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(
        ["replay", str(report), "--witnesses", str(witnesses), "--selection", str(tampered)]
    )

    assert exit_code == ExitCode.REPLAY_MISMATCH
    assert "but the report does not" in capsys.readouterr().err


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


@pytest.fixture
def default_named_docs(scenario_repo: ScenarioRepo, tmp_path: Path) -> Path:
    """One select run whose three documents carry the default names."""
    out = tmp_path / "docs"
    out.mkdir()
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
                str(out / "acquit-report.json"),
                "--selection",
                str(out / "acquit-selection.json"),
                "--witnesses",
                str(out / "acquit-witnesses.json"),
            ]
        )
    assert exit_code == ExitCode.OK
    return out


def test_replay_defaults_resolve_beside_the_report(
    default_named_docs: Path,
    scenario_repo: ScenarioRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # cwd is the repo, the documents live elsewhere: siblings must be found.
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(["replay", str(default_named_docs / "acquit-report.json")])

    assert exit_code == ExitCode.OK
    assert capsys.readouterr().out.strip() == "replay ok: 3 witnesses verified"


def test_replay_cross_checks_the_default_selection_sibling(
    default_named_docs: Path,
    scenario_repo: ScenarioRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = _load(default_named_docs / "acquit-selection.json")
    document["skip"].append({"path": "tests/test_alpha.py", "witness": "w-000099"})
    _dump(document, default_named_docs / "acquit-selection.json")
    monkeypatch.chdir(scenario_repo.path)

    exit_code = main(["replay", str(default_named_docs / "acquit-report.json")])

    assert exit_code == ExitCode.REPLAY_MISMATCH
    assert "but the report does not" in capsys.readouterr().err


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


# ---------------------------------------------------------------------------
# Narrowed witnesses (ADR 0008): replay rebuilds both snapshots
# ---------------------------------------------------------------------------

PKG_INIT = (
    '"""Pure re-exporter."""\n\nfrom .console import Console\nfrom .table import Table\n\n'
    '__all__ = ["Console", "Table"]\n'
)
PKG_TABLE = (
    '"""Inert sibling."""\n\n\nclass Table:\n    def render(self) -> str:\n        return "table"\n'
)
PKG_TABLE_EDIT = (
    '"""Inert sibling."""\n\n\nclass Table:\n    def render(self) -> str:\n        return "grid"\n'
)
PKG_CONSOLE = (
    '"""Not inert."""\n\nSTATE = dict(fancy="*")\n\n\nclass Console:\n'
    '    def banner(self) -> str:\n        return "console"\n'
)
PKG_CONSOLE_EDIT = (
    '"""Not inert."""\n\nSTATE = dict(fancy="!")\n\n\nclass Console:\n'
    '    def banner(self) -> str:\n        return "console"\n'
)


@dataclass(frozen=True)
class NarrowedRepo:
    """A commit chain exercising narrowed selection: base, inert edit, busy edit."""

    path: Path
    base: str
    table_edit: str
    console_edit: str


@pytest.fixture(scope="module")
def narrowed_repo(tmp_path_factory: pytest.TempPathFactory) -> NarrowedRepo:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    repo = init_repo(tmp_path_factory.mktemp("narrowed"))
    write_files(
        repo,
        {
            ".acquit.toml": "narrowing = true\n",
            "pkg/__init__.py": PKG_INIT,
            "pkg/table.py": PKG_TABLE,
            "pkg/console.py": PKG_CONSOLE,
            "free.py": "FREE = 1\n",
            "test_console.py": (
                "from pkg import Console\n\n\ndef test_console():\n    assert Console\n"
            ),
            "test_table.py": "from pkg import Table\n\n\ndef test_table():\n    assert Table\n",
            "test_free.py": module_test_source("free"),
        },
    )
    base = commit_all(repo, "base")
    write_files(repo, {"pkg/table.py": PKG_TABLE_EDIT})
    table_edit = commit_all(repo, "edit inert sibling body")
    write_files(repo, {"pkg/console.py": PKG_CONSOLE_EDIT})
    console_edit = commit_all(repo, "edit busy sibling")
    return NarrowedRepo(path=repo, base=base, table_edit=table_edit, console_edit=console_edit)


def _select_into(repo: Path, base: str, head: str, out: Path) -> tuple[Path, Path]:
    report = out / "report.json"
    witnesses = out / "witnesses.json"
    with pytest.MonkeyPatch.context() as patcher:
        patcher.chdir(repo)
        exit_code = main(
            [
                "select",
                "--base",
                base,
                "--head",
                head,
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


@pytest.fixture(scope="module")
def narrowed_docs(
    narrowed_repo: NarrowedRepo, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, Path]:
    out = tmp_path_factory.mktemp("narrowed-docs")
    return _select_into(narrowed_repo.path, narrowed_repo.base, narrowed_repo.table_edit, out)


def _narrowed_witness_entry(document: dict[str, Any]) -> dict[str, Any]:
    entry = next(w for w in document["witnesses"] if w.get("narrowed"))
    assert isinstance(entry, dict)
    return entry


def test_replay_verifies_narrowed_witnesses(
    narrowed_docs: tuple[Path, Path],
    narrowed_repo: NarrowedRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report, witnesses = narrowed_docs
    document = _load(witnesses)
    entry = _narrowed_witness_entry(document)
    assert entry["test"] == "test_console.py"
    assert entry["claim"] == CLAIM_NARROWED
    monkeypatch.chdir(narrowed_repo.path)

    exit_code = main(["replay", str(report), "--witnesses", str(witnesses)])

    assert exit_code == ExitCode.OK
    assert capsys.readouterr().out.strip() == "replay ok: 2 witnesses verified"


def test_replay_detects_a_tampered_narrowed_blob_sha(
    narrowed_docs: tuple[Path, Path],
    narrowed_repo: NarrowedRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report, witnesses = narrowed_docs
    document = _load(witnesses)
    _narrowed_witness_entry(document)["narrowed"][0]["base_blob"] = "0" * 40
    tampered = _dump(document, tmp_path / "witnesses.json")
    monkeypatch.chdir(narrowed_repo.path)

    exit_code = main(["replay", str(report), "--witnesses", str(tampered)])

    assert exit_code == ExitCode.REPLAY_MISMATCH
    assert "blob sha mismatch for pkg/table.py" in capsys.readouterr().err


def test_replay_detects_a_tampered_init_tier(
    narrowed_docs: tuple[Path, Path],
    narrowed_repo: NarrowedRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report, witnesses = narrowed_docs
    document = _load(witnesses)
    entry = _narrowed_witness_entry(document)
    entry["narrowed"][0]["inits"][0]["base_tier"] = "star-over-literal-all"
    tampered = _dump(document, tmp_path / "witnesses.json")
    monkeypatch.chdir(narrowed_repo.path)

    exit_code = main(["replay", str(report), "--witnesses", str(tampered)])

    assert exit_code == ExitCode.REPLAY_MISMATCH
    assert "relied inits mismatch for pkg/table.py (condition 5)" in capsys.readouterr().err


def test_replay_rejects_a_narrowed_report_without_a_base_sha(
    narrowed_docs: tuple[Path, Path],
    narrowed_repo: NarrowedRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report, witnesses = narrowed_docs
    document = _load(report)
    document["run"]["base_sha"] = None
    tampered = _dump(document, tmp_path / "report.json")
    monkeypatch.chdir(narrowed_repo.path)

    exit_code = main(["replay", str(tampered), "--witnesses", str(witnesses)])

    assert exit_code == ExitCode.REPLAY_MISMATCH
    assert "need a base sha" in capsys.readouterr().err


def test_replay_rejects_forged_inertness_for_a_busy_sibling(
    narrowed_repo: NarrowedRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A genuine run over the console edit refuses narrowing (condition 2).
    # Forge the skip anyway: internally consistent documents claiming the
    # non-inert sibling was import-time-only. Replay must re-derive and refuse.
    repo = narrowed_repo.path
    base, head = narrowed_repo.table_edit, narrowed_repo.console_edit
    report_path, witnesses_path = _select_into(repo, base, head, tmp_path)
    report = _load(report_path)
    witnesses = _load(witnesses_path)

    snapshot = snapshot_tree(head, repo, load_config(repo), load_pytest_config(repo), None)
    closure = import_closure(snapshot.graph, "test_table.py")
    forged_hash = closure_hash(closure)
    witnesses["closures"][forged_hash] = sorted(closure)
    witnesses["witnesses"].append(
        {
            "id": "w-000099",
            "test": "test_table.py",
            "closure": forged_hash,
            "changed": ["pkg/console.py"],
            "claim": CLAIM_NARROWED,
            "narrowed": [
                {
                    "path": "pkg/console.py",
                    "base_blob": blob_shas(base, repo)["pkg/console.py"],
                    "head_blob": blob_shas(head, repo)["pkg/console.py"],
                    "inits": [
                        {
                            "path": "pkg/__init__.py",
                            "base_tier": "strict",
                            "head_tier": "strict",
                        }
                    ],
                }
            ],
        }
    )
    report["tests"]["skipped"].append(
        {"path": "test_table.py", "witness": "w-000099", "narrowed": True}
    )
    forged_report = _dump(report, tmp_path / "forged-report.json")
    forged_witnesses = _dump(witnesses, tmp_path / "forged-witnesses.json")
    monkeypatch.chdir(repo)

    exit_code = main(["replay", str(forged_report), "--witnesses", str(forged_witnesses)])

    assert exit_code == ExitCode.REPLAY_MISMATCH
    assert "not-import-inert" in capsys.readouterr().err
