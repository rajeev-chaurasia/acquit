"""Replay one manifest's PRs: full suites at base and head, acquit at head.

Everything here is imperative shell: git, uv, and pytest run as subprocesses
(never shell=True) inside a scratch workdir. The per-PR JSON written into the
results dir is the study's raw evidence; aggregation never re-runs anything.
A PR whose environment cannot be built or whose base suite will not run is
excluded and recorded, not silently dropped. An unsafe skip fails the run:
the study is itself a test.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from acquit.constants import ENV_CACHE_DIR
from acquit.errors import AcquitError
from acquit.report import digest_report, to_canonical_json
from acquit.study import EXCLUSION_SCHEMA, RESULT_SCHEMA
from acquit.study.compare import SafetyResult, check_safety
from acquit.study.manifest import (
    Manifest,
    PrRecord,
    load_manifest,
    sha256_of_file,
    shard_slice,
    with_exclusion,
    write_manifest,
)
from acquit.study.outcomes import SuiteOutcomes, parse_junit

SUITE_TIMEOUT_SECONDS: Final = 1800.0
_STEP_TIMEOUT_SECONDS: Final = 1800.0

# Suite runs must not pick up ambient nondeterminism from the runner host.
_DETERMINISTIC_ENV: Final = {
    "PYTHONHASHSEED": "0",
    "TZ": "UTC",
    "COLUMNS": "80",
    "TERM": "dumb",
}


class StepFailure(AcquitError):
    """One step of a PR replay failed; the PR is excluded, not the run."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage


@dataclass(frozen=True, slots=True)
class RunSettings:
    """Everything `acquit-study run` resolved from its command line."""

    manifest_path: Path
    workdir: Path
    results_dir: Path
    constraints: Path | None
    quarantine: frozenset[str]
    only_pr: int | None
    shard: tuple[int, int]
    record_exclusions: bool


@dataclass(frozen=True, slots=True)
class SuiteRun:
    outcomes: SuiteOutcomes
    seconds: float


@dataclass(frozen=True, slots=True)
class SelectRun:
    report: dict[str, Any]
    selection: dict[str, Any]
    seconds: float
    replay_verified: bool


def _run_step(
    stage: str,
    args: Sequence[str],
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = _STEP_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(args),
            cwd=cwd,
            env=None if env is None else dict(env),
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except OSError as error:
        raise StepFailure(stage, f"could not execute {args[0]}: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise StepFailure(stage, f"{args[0]} timed out after {int(timeout)}s") from error


def _checked(
    stage: str,
    args: Sequence[str],
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = _STEP_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    completed = _run_step(stage, args, cwd, env=env, timeout=timeout)
    if completed.returncode != 0:
        tail = completed.stderr.decode("utf-8", errors="replace").strip()[-2000:]
        raise StepFailure(stage, f"{' '.join(args)} exited with {completed.returncode}: {tail}")
    return completed


def ensure_mirror(repo_url: str, workdir: Path) -> Path:
    """Clone the repo as a bare mirror, or refresh a reused one."""
    mirror = workdir / "mirror.git"
    if (mirror / "HEAD").exists():
        # A cached mirror may predate recent merges; refresh, tolerate offline.
        _run_step("fetch", ["git", "remote", "update", "--prune"], cwd=mirror)
        return mirror
    _checked("clone", ["git", "clone", "--mirror", repo_url, str(mirror)], cwd=workdir)
    return mirror


def _has_commit(mirror: Path, sha: str) -> bool:
    completed = _run_step("fetch", ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=mirror)
    return completed.returncode == 0


def fetch_pr_commits(mirror: Path, pr: PrRecord) -> None:
    """Make base and head shas available; squash-merged heads need the pull ref."""
    shas = (pr.base_sha, pr.head_sha)
    if all(_has_commit(mirror, sha) for sha in shas):
        return
    pull_ref = f"+refs/pull/{pr.number}/head:refs/pull/{pr.number}/head"
    _run_step("fetch", ["git", "fetch", "--quiet", "origin", pull_ref], cwd=mirror)
    for sha in shas:
        if not _has_commit(mirror, sha):
            _run_step("fetch", ["git", "fetch", "--quiet", "origin", sha], cwd=mirror)
    missing = [sha for sha in shas if not _has_commit(mirror, sha)]
    if missing:
        raise StepFailure("fetch", f"commits not fetchable: {', '.join(missing)}")


def add_worktree(mirror: Path, sha: str, path: Path) -> None:
    remove_worktree(mirror, path)
    args = ["git", "worktree", "add", "--detach", "--force", str(path), sha]
    _checked("worktree", args, cwd=mirror)


def remove_worktree(mirror: Path, path: Path) -> None:
    """Best-effort teardown; leftovers must never fail the next PR."""
    if path.exists():
        _run_step("worktree", ["git", "worktree", "remove", "--force", str(path)], cwd=mirror)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    _run_step("worktree", ["git", "worktree", "prune"], cwd=mirror)


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def make_venv(worktree: Path, python_version: str, constraints: Path | None) -> Path:
    """Build the suite venv: the project itself plus pytest, optionally pinned."""
    venv = worktree / ".study-venv"
    _checked("venv", ["uv", "venv", "--python", python_version, str(venv)], cwd=worktree)
    python = _venv_python(venv)
    install = ["uv", "pip", "install", "--python", str(python), "-e", ".", "pytest"]
    if constraints is not None:
        install += ["--constraints", str(constraints)]
    _checked("install", install, cwd=worktree)
    return python


def _suite_env(workdir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(_DETERMINISTIC_ENV)
    env[ENV_CACHE_DIR] = str(workdir / "acquit-cache")
    return env


def run_suite(stage: str, worktree: Path, python: Path, workdir: Path) -> SuiteRun:
    """Run the full pytest suite once and parse its junit output.

    Exit codes 0 and 1 (tests passed, tests failed) are both usable evidence;
    anything else means the suite itself would not run, which excludes the PR.
    """
    xml_path = worktree / "out.xml"
    args = [
        str(python),
        "-m",
        "pytest",
        "-q",
        "--tb=no",
        f"--junitxml={xml_path}",
        "-p",
        "no:cacheprovider",
    ]
    started = time.monotonic()
    completed = _run_step(
        stage, args, cwd=worktree, env=_suite_env(workdir), timeout=SUITE_TIMEOUT_SECONDS
    )
    seconds = time.monotonic() - started
    if completed.returncode not in (0, 1):
        tail = completed.stdout.decode("utf-8", errors="replace").strip()[-2000:]
        raise StepFailure(stage, f"pytest exited with {completed.returncode}: {tail}")
    try:
        text = xml_path.read_text(encoding="utf-8")
    except OSError as error:
        raise StepFailure(stage, f"pytest wrote no junit xml: {error}") from error
    try:
        outcomes = parse_junit(text)
    except AcquitError as error:
        raise StepFailure(stage, str(error)) from error
    return SuiteRun(outcomes=outcomes, seconds=seconds)


def _read_json_object(stage: str, path: Path) -> dict[str, Any]:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise StepFailure(stage, f"could not read {path.name}: {error}") from error
    if not isinstance(data, dict):
        raise StepFailure(stage, f"{path.name} is not a json object")
    return data


def run_acquit(head_tree: Path, pr: PrRecord, settings: RunSettings) -> SelectRun:
    """Run acquit select and replay via the current installation, timed.

    acquit fails closed: even a nonzero select exit leaves run-all documents
    behind, so the documents are read regardless and their absence is the
    only failure. Replay verification is recorded, never assumed.
    """
    prefix = f"pr-{pr.number:06d}"
    report_path = settings.results_dir / f"{prefix}-report.json"
    selection_path = settings.results_dir / f"{prefix}-selection.json"
    witnesses_path = settings.results_dir / f"{prefix}-witnesses.json"
    env = _suite_env(settings.workdir)
    select_args = [
        sys.executable,
        "-m",
        "acquit",
        "select",
        "--base",
        pr.base_sha,
        "--head",
        pr.head_sha,
        "--report",
        str(report_path),
        "--selection",
        str(selection_path),
        "--witnesses",
        str(witnesses_path),
    ]
    started = time.monotonic()
    _run_step("select", select_args, cwd=head_tree, env=env)
    seconds = time.monotonic() - started
    report = _read_json_object("select", report_path)
    selection = _read_json_object("select", selection_path)
    replay_args = [
        sys.executable,
        "-m",
        "acquit",
        "replay",
        str(report_path),
        "--witnesses",
        str(witnesses_path),
        "--selection",
        str(selection_path),
    ]
    replay = _run_step("replay", replay_args, cwd=head_tree, env=env)
    return SelectRun(
        report=report,
        selection=selection,
        seconds=seconds,
        replay_verified=replay.returncode == 0,
    )


def _skip_paths(selection: Mapping[str, Any]) -> tuple[str, ...]:
    entries = selection.get("skip")
    if not isinstance(entries, list):
        return ()
    paths: list[str] = []
    for entry in entries:
        if isinstance(entry, Mapping) and isinstance(entry.get("path"), str):
            paths.append(entry["path"])
    return tuple(paths)


def _findings_of(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    decision = report.get("decision")
    if not isinstance(decision, Mapping):
        return []
    raw = decision.get("findings")
    if not isinstance(raw, list):
        return []
    return [
        {str(key): value for key, value in entry.items()}
        for entry in raw
        if isinstance(entry, Mapping)
    ]


def _payload(
    pr: PrRecord,
    base_run: SuiteRun,
    head_run: SuiteRun,
    select_run: SelectRun,
    skip_paths: tuple[str, ...],
    safety: SafetyResult,
) -> dict[str, Any]:
    digest = digest_report(select_run.report)
    durations = base_run.outcomes.file_durations
    return {
        "schema": RESULT_SCHEMA,
        "number": pr.number,
        "base_sha": pr.base_sha,
        "head_sha": pr.head_sha,
        "mode": digest.mode,
        "selected": digest.selected,
        "skipped": digest.skipped,
        "always_run": digest.always_run,
        "total": digest.total,
        "findings": _findings_of(select_run.report),
        "skip_paths": list(skip_paths),
        "changed_outcomes": list(safety.changed_outcomes),
        "unsafe_skips": list(safety.unsafe_skips),
        "new_tests_selected": safety.new_tests_selected,
        "replay_verified": select_run.replay_verified,
        "analysis_seconds": round(select_run.seconds, 3),
        "base_suite_seconds": round(base_run.seconds, 3),
        "head_suite_seconds": round(head_run.seconds, 3),
        "per_file_durations": {path: round(durations[path], 3) for path in sorted(durations)},
    }


def replay_pr(
    mirror: Path, manifest: Manifest, pr: PrRecord, settings: RunSettings
) -> dict[str, Any]:
    """Replay one PR end to end and return its result payload."""
    fetch_pr_commits(mirror, pr)
    base_tree = settings.workdir / f"wt-{pr.number}-base"
    head_tree = settings.workdir / f"wt-{pr.number}-head"
    try:
        add_worktree(mirror, pr.base_sha, base_tree)
        base_python = make_venv(base_tree, manifest.python_version, settings.constraints)
        base_run = run_suite("base-suite", base_tree, base_python, settings.workdir)
        add_worktree(mirror, pr.head_sha, head_tree)
        head_python = make_venv(head_tree, manifest.python_version, settings.constraints)
        head_run = run_suite("head-suite", head_tree, head_python, settings.workdir)
        select_run = run_acquit(head_tree, pr, settings)
    finally:
        remove_worktree(mirror, base_tree)
        remove_worktree(mirror, head_tree)
    skip_paths = _skip_paths(select_run.selection)
    safety = check_safety(
        base_run.outcomes.by_test,
        head_run.outcomes.by_test,
        skip_paths,
        settings.quarantine,
    )
    return _payload(pr, base_run, head_run, select_run, skip_paths, safety)


def _verify_constraints(manifest: Manifest, constraints: Path | None) -> None:
    if manifest.constraints_sha256 is None:
        return
    if constraints is None:
        print("study: warning: manifest pins constraints but none were passed", file=sys.stderr)
        return
    digest = sha256_of_file(constraints)
    if digest != manifest.constraints_sha256:
        raise AcquitError(
            f"constraints hash mismatch: manifest pins {manifest.constraints_sha256}, "
            f"{constraints} hashes to {digest}"
        )


def _flagged(payload: Mapping[str, Any]) -> bool:
    unsafe = payload.get("unsafe_skips")
    has_unsafe = isinstance(unsafe, list) and bool(unsafe)
    return has_unsafe or payload.get("new_tests_selected") is False


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(to_canonical_json(payload), encoding="utf-8")


def run_study(settings: RunSettings) -> int:
    """Replay this shard of the manifest; nonzero means an unsafe skip happened."""
    manifest = load_manifest(settings.manifest_path)
    _verify_constraints(manifest, settings.constraints)
    settings.workdir.mkdir(parents=True, exist_ok=True)
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    chosen = manifest.prs
    if settings.only_pr is not None:
        chosen = tuple(pr for pr in chosen if pr.number == settings.only_pr)
        if not chosen:
            raise AcquitError(f"pr {settings.only_pr} is not in the manifest")
    index, count = settings.shard
    chosen = shard_slice(chosen, index, count)
    if not chosen:
        print("study: shard is empty, nothing to do")
        return 0
    mirror = ensure_mirror(manifest.repo_url, settings.workdir)
    excluded_numbers = {entry.number for entry in manifest.excluded}
    unsafe_prs = 0
    for pr in chosen:
        if pr.number in excluded_numbers:
            print(f"study: pr {pr.number}: excluded in manifest, skipping")
            continue
        result_path = settings.results_dir / f"pr-{pr.number:06d}.json"
        if result_path.exists():
            # Reruns stay honest: an existing unsafe result still fails the run.
            existing = _read_json_object("results", result_path)
            if _flagged(existing):
                unsafe_prs += 1
            print(f"study: pr {pr.number}: result already recorded, skipping")
            continue
        try:
            payload = replay_pr(mirror, manifest, pr, settings)
        except StepFailure as error:
            print(f"study: pr {pr.number}: excluded: {error}", file=sys.stderr)
            exclusion = {
                "schema": EXCLUSION_SCHEMA,
                "number": pr.number,
                "stage": error.stage,
                "reason": str(error),
            }
            _write_json(settings.results_dir / f"excluded-{pr.number:06d}.json", exclusion)
            if settings.record_exclusions:
                manifest = with_exclusion(manifest, pr.number, str(error))
                write_manifest(settings.manifest_path, manifest)
            continue
        _write_json(result_path, payload)
        if _flagged(payload):
            unsafe_prs += 1
        verdict = "ok" if payload["replay_verified"] else "MISMATCH"
        print(
            f"study: pr {pr.number}: mode={payload['mode']} "
            f"skipped={payload['skipped']}/{payload['total']} replay={verdict}"
        )
    if unsafe_prs:
        print(
            f"study: FAILED: {unsafe_prs} pr(s) had an unsafe skip or a skipped new test",
            file=sys.stderr,
        )
        return 1
    return 0
