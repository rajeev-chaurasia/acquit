"""Attacks on the composite action wrapper.

The bash body of the select step is lifted verbatim out of action.yml and run
under real bash with the command it shells out to (uvx) replaced by a stub.
Everything else, including the fail-closed fallback and the GITHUB_ENV export,
is the shipped code.
"""

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from adversarial.failclosed_support import (
    action_select_script,
    bash_for,
    outcome,
    read_json,
    run_pytest,
)

SELECTIVE_DOC = (
    '{"schema":"acquit/selection-v2","mode":"selective","graph_hash":"abc",'
    '"tree":{"head_sha":null,"fingerprint":"abc"},'
    '"skip":[{"path":"tests/test_a.py","witness":"w-000001"}]}'
)

UVX_FAILS = 'uvx() { printf "%s\\n" "$*" > "$UVX_ARGV_LOG"; return 1; }'
UVX_WRITES_SELECTIVE = (
    'uvx() { printf "%s\\n" "$*" > "$UVX_ARGV_LOG"; '
    f"printf '{SELECTIVE_DOC}\\n' > \"$SELECTION\"; return 0; }}"
)
PYTHON_WORKS = 'python() { command "$ACQUIT_TEST_PYTHON" "$@"; }'
PYTHON_MISSING = "python() { return 127; }"


@dataclass(frozen=True)
class ActionRun:
    returncode: int
    outputs: Mapping[str, str]
    exported: Mapping[str, str]
    workspace: Path

    @property
    def selection(self) -> Path:
        return self.workspace / "acquit-selection.json"


def _entries(path: Path) -> dict[str, str]:
    """Parse a GITHUB_ENV or GITHUB_OUTPUT file the way the runner does.

    Both the plain NAME=value form and the NAME<<DELIMITER heredoc form are
    understood; lines inside a heredoc block are value content, never keys.
    """
    pairs: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        key, sep, delimiter = line.partition("<<")
        if sep and delimiter:
            value: list[str] = []
            index += 1
            while index < len(lines) and lines[index] != delimiter:
                value.append(lines[index])
                index += 1
            pairs[key] = "\n".join(value)
        else:
            key, sep, plain = line.partition("=")
            if sep:
                pairs[key] = plain
        index += 1
    return pairs


def run_action(
    tmp_path: Path, uvx: str, python: str = PYTHON_WORKS, base_ref: str = ""
) -> ActionRun:
    """Run the shipped select step with stubbed uvx and python."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bash = bash_for(workspace)
    preamble = [f'PWD="{workspace.as_posix()}"', uvx, python, ""]
    script = tmp_path / "select-step.sh"
    script.write_text("\n".join(preamble) + action_select_script(), encoding="utf-8", newline="\n")
    outputs, exported, summary = (
        tmp_path / "outputs.txt",
        tmp_path / "env.txt",
        tmp_path / "summary.md",
    )
    for target in (outputs, exported, summary):
        target.write_text("", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "GITHUB_OUTPUT": outputs.as_posix(),
        "GITHUB_ENV": exported.as_posix(),
        "GITHUB_STEP_SUMMARY": summary.as_posix(),
        "BASE_REF": base_ref,
        "UVX_ARGV_LOG": (tmp_path / "uvx-argv.txt").as_posix(),
        "ACQUIT_TEST_PYTHON": Path(sys.executable).as_posix(),
    }
    completed = subprocess.run(
        [bash, script.as_posix()],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return ActionRun(completed.returncode, _entries(outputs), _entries(exported), workspace)


def test_fallback_document_is_a_valid_run_all_selection(tmp_path: Path) -> None:
    run = run_action(tmp_path, UVX_FAILS)

    document = read_json(run.selection)
    assert document == {
        "schema": "acquit/selection-v2",
        "mode": "run-all",
        "graph_hash": None,
        "tree": {"head_sha": None, "fingerprint": None},
        "skip": [],
    }
    assert run.outputs["mode"] == "run-all"
    assert run.exported["ACQUIT_SELECTION_FILE"] == run.outputs["selection"]


def test_fallback_document_makes_the_plugin_run_every_test(tmp_path: Path) -> None:
    run = run_action(tmp_path, UVX_FAILS)
    project = run.workspace / "tests"
    project.mkdir(parents=True, exist_ok=True)
    for name in ("test_a.py", "test_b.py"):
        (project / name).write_text(f"def {name[:-3]}():\n    assert True\n", encoding="utf-8")

    result = run_pytest(run.workspace, run.selection)

    assert result.returncode == 0, outcome(result)
    assert "2 passed" in result.stdout, outcome(result)


def test_empty_base_ref_falls_back_to_origin_main(tmp_path: Path) -> None:
    """github.base_ref is empty on push events; the default branch is assumed."""
    run_action(tmp_path, UVX_FAILS, base_ref="")

    argv = (tmp_path / "uvx-argv.txt").read_text(encoding="utf-8")
    assert "--base origin/main" in argv


def test_provided_base_ref_reaches_the_cli(tmp_path: Path) -> None:
    run_action(tmp_path, UVX_FAILS, base_ref="release/2.0")

    argv = (tmp_path / "uvx-argv.txt").read_text(encoding="utf-8")
    assert "--base origin/release/2.0" in argv


def test_mode_output_never_contradicts_the_exported_selection(tmp_path: Path) -> None:
    """ADV-FC-10: the mode is read from the document itself, no probe involved."""
    run = run_action(tmp_path, UVX_WRITES_SELECTIVE, python=PYTHON_MISSING)

    assert read_json(run.selection)["mode"] == "selective"
    assert run.exported["ACQUIT_SELECTION_FILE"] == run.outputs["selection"]
    assert run.outputs["mode"] == "selective", run.outputs


def test_mode_output_follows_the_document(tmp_path: Path) -> None:
    run = run_action(tmp_path, UVX_WRITES_SELECTIVE)

    assert run.outputs["mode"] == "selective"
    assert read_json(run.selection)["skip"] == [{"path": "tests/test_a.py", "witness": "w-000001"}]


def test_github_env_export_cannot_define_extra_variables(tmp_path: Path) -> None:
    """ADV-FC-11: a newline in the exported value never becomes a second variable."""
    bash = bash_for(tmp_path)
    export_line = next(
        line for line in action_select_script().splitlines() if "$GITHUB_ENV" in line
    )
    exported = tmp_path / "env.txt"
    exported.write_text("", encoding="utf-8")
    script = tmp_path / "export.sh"
    script.write_text(
        f'GITHUB_ENV="{exported.as_posix()}"\n'
        "SELECTION=$'/workspace/hostile\\nACQUIT_INJECTED=1'\n"
        f"{export_line}\n",
        encoding="utf-8",
        newline="\n",
    )
    subprocess.run([bash, script.as_posix()], check=True, capture_output=True)

    assert set(_entries(exported)) == {"ACQUIT_SELECTION_FILE"}, exported.read_text(
        encoding="utf-8"
    )
