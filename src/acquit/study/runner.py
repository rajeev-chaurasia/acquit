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

import acquit
from acquit import vcs
from acquit.constants import ENV_CACHE_DIR, ENV_CANARY, ENV_SELECTION_FILE
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
from acquit.study.mutate import Mutant, detection_parity, enumerate_mutants
from acquit.study.outcomes import Outcome, SuiteOutcomes, parse_junit

SUITE_TIMEOUT_SECONDS: Final = 1800.0
_STEP_TIMEOUT_SECONDS: Final = 1800.0
# The per-suite timeout scaled down for mutant runs: a mutant that hangs the
# suite is recorded as a failed run and skipped, never fatal to the PR.
_MUTANT_TIMEOUT_SECONDS: Final = 600.0

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
    # Mutation-injection arm: up to this many first-order mutants per PR.
    mutants: int = 0


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


def install_args(python: Path, suite_deps: Sequence[str], constraints: Path | None) -> list[str]:
    """Assemble the uv pip install command for one suite venv.

    suite_deps come from the manifest: extra requirements the target's test
    suite needs just to collect, part of the frozen recipe.
    """
    args = ["uv", "pip", "install", "--python", str(python), "-e", ".", "pytest", *suite_deps]
    if constraints is not None:
        args += ["--constraints", str(constraints)]
    return args


def make_venv(
    worktree: Path, python_version: str, suite_deps: Sequence[str], constraints: Path | None
) -> Path:
    """Build the suite venv: the project, pytest, and suite deps, optionally pinned."""
    venv = worktree / ".study-venv"
    _checked("venv", ["uv", "venv", "--python", python_version, str(venv)], cwd=worktree)
    python = _venv_python(venv)
    _checked("install", install_args(python, suite_deps, constraints), cwd=worktree)
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


def _changed_python_files(head_tree: Path, pr: PrRecord) -> tuple[str, ...]:
    """The PR's changed .py files still present at head: the mutation targets."""
    changes = vcs.changed_files(pr.base_sha, pr.head_sha, head_tree)
    return tuple(
        sorted(
            change.path
            for change in changes
            if change.status is not vcs.ChangeStatus.DELETED
            and change.path.endswith(".py")
            and (head_tree / change.path).is_file()
        )
    )


def plan_mutants(
    per_file: Mapping[str, Sequence[Mutant]], budget: int
) -> tuple[tuple[str, Mutant], ...]:
    """Interleave files round-robin so one mutant-rich file cannot eat the budget."""
    ordered = [(path, tuple(per_file[path])) for path in sorted(per_file) if per_file[path]]
    picked: list[tuple[str, Mutant]] = []
    depth = 0
    while len(picked) < budget:
        row = [(path, mutants[depth]) for path, mutants in ordered if depth < len(mutants)]
        if not row:
            break
        picked.extend(row[: budget - len(picked)])
        depth += 1
    return tuple(picked)


def _suite_is_green(outcomes: SuiteOutcomes) -> bool:
    bad = {Outcome.FAILED, Outcome.ERROR}
    return all(not (values & bad) for values in outcomes.by_test.values())


def _acquit_project_root() -> Path | None:
    """The acquit checkout this study is running from, when there is one.

    The suite venv deliberately excludes acquit, but applying a selection
    needs the pytest plugin importable there. The study normally runs via uv
    from the repo, so the package sits in src/ next to pyproject.toml; a
    wheel install has nothing to build from and the arm declines loudly.
    """
    package_file = acquit.__file__
    if package_file is None:
        return None
    source_root = Path(package_file).resolve().parent.parent
    project = source_root.parent
    marker = project / "pyproject.toml"
    if source_root.name != "src" or not marker.is_file():
        return None
    try:
        named = 'name = "acquit"' in marker.read_text(encoding="utf-8")
    except OSError:
        return None
    return project if named else None


def _install_selection_plugin(python: Path, worktree: Path) -> None:
    project = _acquit_project_root()
    if project is None:
        raise StepFailure(
            "mutants", "acquit is not running from a src checkout; cannot install the plugin"
        )
    args = ["uv", "pip", "install", "--python", str(python), str(project)]
    _checked("mutants", args, cwd=worktree)


def _rebound_selection(selection: Mapping[str, Any], head_tree: Path, out_path: Path) -> None:
    """Write a copy of the captured selection bound to the mutated tree.

    The plugin refuses a selection whose tree fingerprint has moved on, which
    is right in production and wrong here: the mutant is the divergence under
    test. Only the fingerprint is recomputed, with the plugin's own exclusion
    semantics; the skip set stays exactly what acquit proved for the PR.
    """
    artifacts = selection.get("artifacts")
    exclude = (
        frozenset(value for value in artifacts.values() if isinstance(value, str))
        if isinstance(artifacts, Mapping)
        else frozenset()
    )
    root = vcs.repo_root(head_tree).resolve()
    fingerprint = vcs.working_tree_fingerprint(root, exclude)
    document = dict(selection)
    tree = document.get("tree")
    rebound = dict(tree) if isinstance(tree, Mapping) else {}
    rebound["fingerprint"] = fingerprint
    document["tree"] = rebound
    out_path.write_text(to_canonical_json(document), encoding="utf-8")


def _run_mutant_suite(
    stage: str,
    worktree: Path,
    python: Path,
    workdir: Path,
    selection_file: Path | None,
    stop_on_first: bool,
) -> tuple[bool, float, str]:
    """One pytest run against the mutated tree: (killed, seconds, stdout).

    Exit 0 means the mutant survived. Exit 1 (test failures) and exit 2
    (collection interrupted, the import-breaking mutants) both mean the run
    caught it. Anything else is a broken run, which excludes the mutant.
    """
    env = _suite_env(workdir)
    # a fresh .pyc would change the tree between rebinding and verification
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop(ENV_CANARY, None)
    env.pop(ENV_SELECTION_FILE, None)
    if selection_file is not None:
        env[ENV_SELECTION_FILE] = str(selection_file)
    args = [str(python), "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"]
    if stop_on_first:
        args.append("-x")
    started = time.monotonic()
    completed = _run_step(stage, args, cwd=worktree, env=env, timeout=_MUTANT_TIMEOUT_SECONDS)
    seconds = time.monotonic() - started
    stdout = completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode not in (0, 1, 2):
        tail = stdout.strip()[-2000:]
        raise StepFailure(stage, f"pytest exited with {completed.returncode}: {tail}")
    return completed.returncode != 0, seconds, stdout


def _applied_status(stdout: str) -> str | None:
    """The plugin's one status line, printed even under -q; None if absent."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("acquit:") and "selection" in stripped:
            return stripped
    return None


def _mutants_block(
    requested: int, entries: list[dict[str, Any]], errors: list[dict[str, Any]]
) -> dict[str, Any]:
    kills = [
        (bool(entry["killed_by_selected"]), bool(entry["killed_by_full"])) for entry in entries
    ]
    return {
        "requested": requested,
        "entries": entries,
        "errors": errors,
        "detection_parity": round(detection_parity(kills), 4),
    }


def run_mutation_arm(
    head_tree: Path,
    python: Path,
    pr: PrRecord,
    head_run: SuiteRun,
    select_run: SelectRun,
    settings: RunSettings,
) -> dict[str, Any]:
    """Inject up to --mutants first-order mutants and record who kills them.

    Per mutant: the acquit-selected set runs first (the captured head
    selection applied through the real pytest plugin, re-bound to the mutated
    tree), then the full suite with -x. The selected set must kill whatever
    the full suite kills. Per-mutant failures are recorded and skipped.
    """
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not _suite_is_green(head_run.outcomes):
        # a pre-existing failure would count as a kill for every mutant
        errors.append({"stage": "mutants", "reason": "head suite is not green, kills ambiguous"})
        return _mutants_block(settings.mutants, entries, errors)
    try:
        per_file: dict[str, list[Mutant]] = {}
        for path in _changed_python_files(head_tree, pr):
            try:
                source = (head_tree / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                errors.append({"stage": "mutant-read", "file": path, "reason": str(error)})
                continue
            per_file[path] = enumerate_mutants(source)
        plan = plan_mutants(per_file, settings.mutants)
        if plan:
            _install_selection_plugin(python, head_tree)
    except (StepFailure, AcquitError) as error:
        errors.append({"stage": "mutants", "reason": str(error)})
        return _mutants_block(settings.mutants, entries, errors)
    selective = select_run.selection.get("mode") == "selective"
    rebound_path = settings.workdir / f"pr-{pr.number:06d}-mutant-selection.json"
    for path, mutant in plan:
        target_file = head_tree / path
        record: dict[str, Any] = {
            "file": path,
            "line": mutant.line,
            "col": mutant.col,
            "kind": str(mutant.kind),
        }
        try:
            original = target_file.read_bytes()
        except OSError as error:
            errors.append({**record, "stage": "mutant-apply", "reason": str(error)})
            continue
        outcome: tuple[bool, float, bool, float] | None = None
        restored = True
        try:
            target_file.write_text(mutant.source, encoding="utf-8")
            _rebound_selection(select_run.selection, head_tree, rebound_path)
            selected_killed, selected_seconds, stdout = _run_mutant_suite(
                "mutant-selected", head_tree, python, settings.workdir, rebound_path, False
            )
            status = _applied_status(stdout)
            if selective and (status is None or "applied" not in status):
                # a refused selection runs everything and would fake parity
                raise StepFailure(
                    "mutant-selected",
                    f"selection was not applied: {status or 'no acquit status line'}",
                )
            full_killed, full_seconds, _ = _run_mutant_suite(
                "mutant-full", head_tree, python, settings.workdir, None, True
            )
            outcome = (selected_killed, selected_seconds, full_killed, full_seconds)
        except (StepFailure, AcquitError, OSError) as error:
            errors.append({**record, "stage": "mutant-run", "reason": str(error)})
        finally:
            try:
                target_file.write_bytes(original)
            except OSError as error:
                errors.append({**record, "stage": "mutant-restore", "reason": str(error)})
                restored = False
        if not restored:
            # a tree stuck mutated would poison every following measurement
            break
        if outcome is None:
            continue
        selected_killed, selected_seconds, full_killed, full_seconds = outcome
        entries.append(
            {
                **record,
                "description": mutant.description,
                "killed_by_selected": selected_killed,
                "killed_by_full": full_killed,
                "selected_seconds": round(selected_seconds, 3),
                "full_seconds": round(full_seconds, 3),
            }
        )
    return _mutants_block(settings.mutants, entries, errors)


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
    mutants: dict[str, Any] | None = None,
) -> dict[str, Any]:
    digest = digest_report(select_run.report)
    durations = base_run.outcomes.file_durations
    payload: dict[str, Any] = {
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
    if mutants is not None:
        payload["mutants"] = mutants
    return payload


def replay_pr(
    mirror: Path, manifest: Manifest, pr: PrRecord, settings: RunSettings
) -> dict[str, Any]:
    """Replay one PR end to end and return its result payload."""
    fetch_pr_commits(mirror, pr)
    base_tree = settings.workdir / f"wt-{pr.number}-base"
    head_tree = settings.workdir / f"wt-{pr.number}-head"
    mutants_block: dict[str, Any] | None = None
    try:
        add_worktree(mirror, pr.base_sha, base_tree)
        base_python = make_venv(
            base_tree, manifest.python_version, manifest.suite_deps, settings.constraints
        )
        base_run = run_suite("base-suite", base_tree, base_python, settings.workdir)
        add_worktree(mirror, pr.head_sha, head_tree)
        head_python = make_venv(
            head_tree, manifest.python_version, manifest.suite_deps, settings.constraints
        )
        head_run = run_suite("head-suite", head_tree, head_python, settings.workdir)
        select_run = run_acquit(head_tree, pr, settings)
        if settings.mutants > 0:
            mutants_block = run_mutation_arm(
                head_tree, head_python, pr, head_run, select_run, settings
            )
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
    return _payload(pr, base_run, head_run, select_run, skip_paths, safety, mutants_block)


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
        block = payload.get("mutants")
        if isinstance(block, dict):
            ran = [entry for entry in block.get("entries", []) if isinstance(entry, Mapping)]
            full_kills = sum(1 for entry in ran if entry.get("killed_by_full") is True)
            missed = sum(
                1
                for entry in ran
                if entry.get("killed_by_full") is True
                and entry.get("killed_by_selected") is not True
            )
            print(
                f"study: pr {pr.number}: mutants ran={len(ran)} "
                f"killed_by_full={full_kills} missed={missed}"
            )
    if unsafe_prs:
        print(
            f"study: FAILED: {unsafe_prs} pr(s) had an unsafe skip or a skipped new test",
            file=sys.stderr,
        )
        return 1
    return 0
