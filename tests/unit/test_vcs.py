import shutil
import subprocess
from pathlib import Path

import pytest

from acquit.errors import VcsError
from acquit.vcs import (
    ChangedFile,
    ChangeStatus,
    _parse_name_status,
    blob_shas,
    changed_files,
    list_files,
    merge_base,
    read_blob,
    repo_root,
    resolve_sha,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return completed.stdout.strip()


def _commit_all(repo_path: Path, message: str) -> str:
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", message)
    return _git(repo_path, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "Acquit Tests")
    _git(tmp_path, "config", "user.email", "tests@acquit.invalid")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


def test_repo_root_from_subdirectory(repo: Path) -> None:
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    _commit_all(repo, "base")
    sub = repo / "pkg"
    sub.mkdir()
    assert repo_root(sub).resolve() == repo.resolve()


def test_changed_files_added_modified_deleted(repo: Path) -> None:
    (repo / "keep.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "gone.py").write_text("y = 2\n", encoding="utf-8")
    base = _commit_all(repo, "base")
    (repo / "keep.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "gone.py").unlink()
    (repo / "new.py").write_text("z = 3\n", encoding="utf-8")
    head = _commit_all(repo, "head")

    by_path = {change.path: change for change in changed_files(base, head, repo)}
    assert by_path["new.py"] == ChangedFile(path="new.py", status=ChangeStatus.ADDED)
    assert by_path["keep.py"] == ChangedFile(path="keep.py", status=ChangeStatus.MODIFIED)
    assert by_path["gone.py"] == ChangedFile(path="gone.py", status=ChangeStatus.DELETED)


def test_changed_files_detects_rename(repo: Path) -> None:
    content = "".join(f"line = {i}\n" for i in range(50))
    src = repo / "src"
    src.mkdir()
    (src / "old_name.py").write_text(content, encoding="utf-8")
    base = _commit_all(repo, "base")
    (src / "old_name.py").rename(src / "new_name.py")
    head = _commit_all(repo, "head")

    (only,) = changed_files(base, head, repo)
    assert only == ChangedFile(
        path="src/new_name.py", status=ChangeStatus.RENAMED, old_path="src/old_name.py"
    )


def test_worktree_diff_and_untracked_listing(repo: Path) -> None:
    (repo / "tracked.py").write_text("a = 1\n", encoding="utf-8")
    base = _commit_all(repo, "base")
    (repo / "tracked.py").write_text("a = 2\n", encoding="utf-8")
    (repo / "untracked.py").write_text("b = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("noise\n", encoding="utf-8")

    changes = changed_files(base, None, repo)
    assert changes == (ChangedFile(path="tracked.py", status=ChangeStatus.MODIFIED),)

    files = list_files(None, repo)
    assert "tracked.py" in files
    assert "untracked.py" in files
    assert "ignored.txt" not in files


def test_list_files_at_ref(repo: Path) -> None:
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("a = 1\n", encoding="utf-8")
    head = _commit_all(repo, "base")
    (pkg / "later.py").write_text("b = 2\n", encoding="utf-8")

    assert list_files(head, repo) == ("pkg/mod.py",)


def test_read_blob_round_trip(repo: Path) -> None:
    payload = b"data = b'\x00\xff'\n"
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_bytes(payload)
    head = _commit_all(repo, "base")

    assert read_blob(head, "pkg/mod.py", repo) == payload


def test_read_blob_missing_path_raises(repo: Path) -> None:
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    head = _commit_all(repo, "base")
    with pytest.raises(VcsError):
        read_blob(head, "nope.py", repo)


def test_blob_shas_maps_path_to_sha(repo: Path) -> None:
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    head = _commit_all(repo, "base")

    shas = blob_shas(head, repo)
    assert shas == {"a.py": _git(repo, "rev-parse", f"{head}:a.py")}


def test_merge_base_finds_fork_point(repo: Path) -> None:
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    fork = _commit_all(repo, "fork point")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "b.py").write_text("b = 1\n", encoding="utf-8")
    _commit_all(repo, "feature work")
    _git(repo, "checkout", "-q", "main")
    (repo / "c.py").write_text("c = 1\n", encoding="utf-8")
    _commit_all(repo, "main work")

    assert merge_base("main", "feature", repo) == fork


def test_resolve_sha_expands_ref(repo: Path) -> None:
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    head = _commit_all(repo, "base")
    assert resolve_sha("HEAD", repo) == head


def test_bad_ref_raises_vcs_error(repo: Path) -> None:
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    _commit_all(repo, "base")
    with pytest.raises(VcsError):
        resolve_sha("does-not-exist", repo)


def test_non_repo_directory_raises_vcs_error(tmp_path: Path) -> None:
    with pytest.raises(VcsError):
        repo_root(tmp_path)


def test_parse_name_status_copies_and_type_changes() -> None:
    raw = "C100\0src/a.py\0src/b.py\0T\0hook.py\0R087\0old.py\0new.py\0"
    assert _parse_name_status(raw) == (
        ChangedFile(path="src/b.py", status=ChangeStatus.ADDED),
        ChangedFile(path="hook.py", status=ChangeStatus.MODIFIED),
        ChangedFile(path="new.py", status=ChangeStatus.RENAMED, old_path="old.py"),
    )


def test_parse_name_status_rejects_garbage() -> None:
    with pytest.raises(VcsError):
        _parse_name_status("Z\0what.py\0")
    with pytest.raises(VcsError):
        _parse_name_status("R100\0only-one-path\0")
