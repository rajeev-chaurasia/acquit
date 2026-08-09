"""Attacks on the analysis shell: git states, the parse cache, and replay.

These reproductions never touch the pure core. They ask a narrower question:
when the world around the analysis is degraded or hostile, does the delivered
selection document still deserve to be trusted?
"""

import os
import stat
import subprocess
from collections.abc import Iterator
from contextlib import chdir, contextmanager
from pathlib import Path

import pytest

from acquit.cli import main
from acquit.constants import ENV_CACHE_DIR
from acquit.errors import ExitCode
from acquit.graph.cache import CACHE_FORMAT_VERSION, facts_to_dict, parse_cache_dir
from acquit.graph.parse import ModuleFacts
from acquit.graph.resolvers.checkers import ReexportScan
from adversarial.failclosed_support import (
    commit,
    deselected_count,
    git,
    new_repo,
    outcome,
    read_json,
    replay,
    run_pytest,
    select,
    skip_paths,
    two_module_repo,
    write,
    write_json,
)

# The legacy in-checkout location, kept to prove it is no longer trusted.
IN_REPO_PARSE_CACHE = Path(".acquit/cache/parse")


def poison_cache(cache_dir: Path, blob_sha: str, path: str) -> Path:
    """Claim, for one blob, that the file it holds imports nothing."""
    facts = ModuleFacts(
        path=path,
        imports=(),
        dyn_literal_imports=(),
        folded_dynamic_imports=(),
        suspects=(),
        defines_module_getattr=False,
        pytest_plugins_decl=(),
        reexport=ReexportScan(reason=None, bindings=(), stars=(), local_names=(), all_names=None),
        inert_reason=None,
        bound_names=(),
    )
    entry = cache_dir / f"{blob_sha}.json"
    entry.parent.mkdir(parents=True, exist_ok=True)
    return write_json(entry, {"version": CACHE_FORMAT_VERSION, "facts": facts_to_dict(facts)})


def alpha_change_repo(path: Path) -> tuple[Path, str, str]:
    repo = two_module_repo(path)
    base = commit(repo, "base")
    write(repo, {"alpha.py": "ALPHA = 2\n"})
    return repo, base, commit(repo, "change alpha")


def git_expect_failure(repo: Path, *args: str) -> int:
    completed = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    assert completed.returncode != 0, completed.stdout
    return completed.returncode


def select_with_durations(repo: Path, out: Path, base: str, head: str, durations: Path) -> int:
    with chdir(repo):
        return main(
            [
                "select",
                "--base",
                base,
                "--head",
                head,
                "--report",
                str(out / "report.json"),
                "--selection",
                str(out / "selection.json"),
                "--witnesses",
                str(out / "witnesses.json"),
                "--durations",
                str(durations),
            ]
        )


def test_poisoned_in_repo_parse_cache_cannot_forge_a_skip(tmp_path: Path) -> None:
    """ADV-FC-7: a crafted cache entry inside the workspace is never read."""
    repo, base, head = alpha_change_repo(tmp_path / "repo")
    out = tmp_path / "out"
    assert select(repo, out, base, head) == ExitCode.OK
    assert skip_paths(read_json(out / "selection.json")) == ["tests/test_beta.py"]

    blob = git(repo, "rev-parse", f"{head}:tests/test_alpha.py")
    poison_cache(repo / IN_REPO_PARSE_CACHE, blob, "tests/test_alpha.py")
    poisoned = tmp_path / "poisoned"
    assert select(repo, poisoned, base, head) == ExitCode.OK

    document = read_json(poisoned / "selection.json")
    assert "tests/test_alpha.py" not in skip_paths(document), document


def test_replay_catches_the_poisoned_parse_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evidence layer refuses the forgery the selection layer accepted."""
    repo, base, head = alpha_change_repo(tmp_path / "repo")
    # The cache acquit actually trusts (a restored CI cache, say) is poisoned.
    monkeypatch.setenv(ENV_CACHE_DIR, str(tmp_path / "cache"))
    blob = git(repo, "rev-parse", f"{head}:tests/test_alpha.py")
    poison_cache(parse_cache_dir(repo), blob, "tests/test_alpha.py")
    out = tmp_path / "out"
    assert select(repo, out, base, head) == ExitCode.OK
    assert "tests/test_alpha.py" in skip_paths(read_json(out / "selection.json"))

    assert replay(repo, out) == ExitCode.REPLAY_MISMATCH


def test_replay_verifies_the_document_that_drives_pytest(tmp_path: Path) -> None:
    """ADV-FC-8: replay cross-checks the artifact that actually deselects tests."""
    repo, base, head = alpha_change_repo(tmp_path / "repo")
    out = tmp_path / "out"
    assert select(repo, out, base, head) == ExitCode.OK
    selection = out / "selection.json"
    document = read_json(selection)
    document["skip"] = document["skip"] + [{"path": "tests/test_alpha.py", "witness": "w-000666"}]
    write_json(selection, document)

    verdict = replay(repo, out)

    result = run_pytest(repo, selection)
    assert deselected_count(result) == 2, outcome(result)
    assert verdict == ExitCode.REPLAY_MISMATCH


def test_shallow_clone_without_the_base_writes_a_run_all_document(tmp_path: Path) -> None:
    """fetch-depth 1 is the actions/checkout default; the base is simply absent."""
    repo, _, _ = alpha_change_repo(tmp_path / "repo")
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", repo.as_uri(), str(shallow))
    out = tmp_path / "out"

    assert select(shallow, out, "HEAD~1") == ExitCode.INTERNAL

    selection = read_json(out / "selection.json")
    assert selection["mode"] == "run-all"
    assert selection["skip"] == []
    assert read_json(out / "report.json")["decision"]["mode"] == "run-all"


@pytest.mark.parametrize("style", ["tag", "branch", "sha", "detached"])
def test_every_base_ref_style_agrees(tmp_path: Path, style: str) -> None:
    repo, base, head = alpha_change_repo(tmp_path / "repo")
    git(repo, "tag", "v1", base)
    git(repo, "branch", "release", base)
    if style == "detached":
        git(repo, "checkout", "-q", "--detach", head)
    ref = {"tag": "v1", "branch": "release", "sha": base, "detached": base}[style]
    out = tmp_path / "out"

    assert select(repo, out, ref, head) == ExitCode.OK

    assert skip_paths(read_json(out / "selection.json")) == ["tests/test_beta.py"]


def test_conflicted_merge_never_skips_the_conflicted_module(tmp_path: Path) -> None:
    repo = two_module_repo(tmp_path / "repo")
    base = commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo, {"alpha.py": "ALPHA = 2\n"})
    commit(repo, "feature alpha")
    git(repo, "checkout", "-q", "main")
    write(repo, {"alpha.py": "ALPHA = 3\n"})
    commit(repo, "main alpha")
    git_expect_failure(repo, "merge", "feature", "-q")
    assert "<<<<<<<" in (repo / "alpha.py").read_text(encoding="utf-8")

    out = tmp_path / "out"
    assert select(repo, out, base) == ExitCode.OK

    document = read_json(out / "selection.json")
    assert "tests/test_alpha.py" not in skip_paths(document), document
    rules = {finding["rule"] for finding in read_json(out / "report.json")["decision"]["findings"]}
    assert "R010" in rules, rules


def test_submodule_pointer_bump_forces_run_all(tmp_path: Path) -> None:
    library = new_repo(tmp_path / "library")
    write(library, {"libmod.py": "VALUE = 1\n"})
    commit(library, "library base")
    repo, _, _ = alpha_change_repo(tmp_path / "repo")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", library.as_uri(), "lib")
    with_submodule = commit(repo, "vendor the library")
    write(library, {"libmod.py": "VALUE = 2\n"})
    bumped = commit(library, "library change")
    git(repo / "lib", "fetch", "-q", "origin")
    git(repo / "lib", "checkout", "-q", bumped)
    bump = commit(repo, "bump the submodule")

    out = tmp_path / "out"
    assert select(repo, out, with_submodule, bump) == ExitCode.OK

    selection = read_json(out / "selection.json")
    assert selection["mode"] == "run-all"
    assert selection["skip"] == []
    assert bump != with_submodule


@pytest.mark.parametrize("autocrlf", ["false", "true"])
def test_autocrlf_variance_does_not_change_the_decision(tmp_path: Path, autocrlf: str) -> None:
    repo = two_module_repo(tmp_path / "repo")
    git(repo, "config", "core.autocrlf", autocrlf)
    base = commit(repo, "base")
    git(repo, "rm", "-q", "--cached", "-r", ".")
    git(repo, "reset", "-q", "--hard")
    write(repo, {"alpha.py": "ALPHA = 2\n"})
    out = tmp_path / "out"

    assert select(repo, out, base) == ExitCode.OK

    document = read_json(out / "selection.json")
    assert skip_paths(document) == ["tests/test_beta.py"]


@contextmanager
def read_only(path: Path) -> Iterator[Path]:
    original = path.stat().st_mode
    path.chmod(stat.S_IREAD)
    try:
        yield path
    finally:
        path.chmod(original)


def test_failed_select_never_leaves_a_selective_document(tmp_path: Path) -> None:
    """ADV-FC-9: the failure path converts the selection document, loudly."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:  # pragma: no cover - CI containers
        pytest.skip("a read-only file does not stop root")
    repo, base, head = alpha_change_repo(tmp_path / "repo")
    out = tmp_path / "out"
    assert select(repo, out, base, head) == ExitCode.OK
    selection = out / "selection.json"
    assert read_json(selection)["mode"] == "selective"
    durations = tmp_path / "durations.json"
    durations.write_text("{not json", encoding="utf-8")

    with read_only(selection):
        code = select_with_durations(repo, out, base, head, durations)

        assert code == ExitCode.INTERNAL
        assert read_json(selection)["mode"] == "run-all", selection.read_text(encoding="utf-8")


def test_malformed_report_makes_replay_fail_loudly(tmp_path: Path) -> None:
    """A doctored report must never let replay exit zero."""
    repo, base, head = alpha_change_repo(tmp_path / "repo")
    out = tmp_path / "out"
    assert select(repo, out, base, head) == ExitCode.OK
    report = read_json(out / "report.json")
    report["tests"]["skipped"] = [{"path": "tests/test_beta.py"}]
    write_json(out / "report.json", report)

    assert replay(repo, out) != ExitCode.OK


def test_select_from_a_subdirectory_emits_repo_relative_paths(tmp_path: Path) -> None:
    repo, base, head = alpha_change_repo(tmp_path / "repo")
    out = tmp_path / "out"

    assert select(repo / "tests", out, base, head) == ExitCode.OK

    assert skip_paths(read_json(out / "selection.json")) == ["tests/test_beta.py"]


def test_untracked_artifacts_in_the_workspace_do_not_change_the_diff(tmp_path: Path) -> None:
    """The action leaves its documents in the workspace; a rerun must ignore them."""
    repo, base, _ = alpha_change_repo(tmp_path / "repo")
    (repo / "acquit-report.json").write_text("{}", encoding="utf-8")
    (repo / IN_REPO_PARSE_CACHE).mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out"

    assert select(repo, out, base) == ExitCode.OK

    report = read_json(out / "report.json")
    assert [entry["path"] for entry in report["changed"]] == ["alpha.py"]
    assert skip_paths(read_json(out / "selection.json")) == ["tests/test_beta.py"]
