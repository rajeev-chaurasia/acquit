"""Git plumbing behind a narrow, typed surface.

Every helper shells out to git (never shell=True) and converts any failure,
including a missing git binary, into VcsError carrying the command and stderr.
All returned paths are repo-relative POSIX, matching graph node identity.
"""

import hashlib
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from acquit.errors import VcsError

# Gitlinks (submodules) carry no blob content and never enter fingerprints.
_GITLINK_MODE = "160000"


class ChangeStatus(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass(frozen=True, slots=True)
class ChangedFile:
    # Path at head; for renames this is the new path.
    path: str
    status: ChangeStatus
    old_path: str | None = None


_SINGLE_PATH_STATUS = {
    "A": ChangeStatus.ADDED,
    "M": ChangeStatus.MODIFIED,
    "D": ChangeStatus.DELETED,
    # A type change (regular file <-> symlink) still changes content.
    "T": ChangeStatus.MODIFIED,
}


def _run_git(args: Sequence[str], cwd: Path) -> bytes:
    command = ["git", *args]
    try:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, check=False)
    except OSError as error:
        raise VcsError(f"could not execute {' '.join(command)}: {error}") from error
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise VcsError(f"{' '.join(command)} exited with {completed.returncode}: {stderr}")
    return completed.stdout


def _run_git_text(args: Sequence[str], cwd: Path) -> str:
    stdout = _run_git(args, cwd)
    try:
        return stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise VcsError(f"git {args[0]} produced non-UTF-8 output: {error}") from error


def repo_root(cwd: Path) -> Path:
    """Return the working tree root of the repository containing cwd."""
    return Path(_run_git_text(["rev-parse", "--show-toplevel"], cwd).strip())


def merge_base(base_ref: str, head_ref: str, cwd: Path) -> str:
    """Return the sha of the merge base of the two refs."""
    return _run_git_text(["merge-base", base_ref, head_ref], cwd).strip()


def _parse_name_status(raw: str) -> tuple[ChangedFile, ...]:
    tokens = raw.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    changes: list[ChangedFile] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        code, score = status[:1], status[1:]
        if score and not score.isdigit():
            raise VcsError(f"unparseable diff status {status!r}")
        if code in ("R", "C"):
            if index + 2 >= len(tokens):
                raise VcsError(f"truncated diff record after status {status!r}")
            old_path, new_path = tokens[index + 1], tokens[index + 2]
            index += 3
            if code == "R":
                changes.append(
                    ChangedFile(path=new_path, status=ChangeStatus.RENAMED, old_path=old_path)
                )
            else:
                # A copy leaves the source untouched; only the new path is a change.
                changes.append(ChangedFile(path=new_path, status=ChangeStatus.ADDED))
        elif code in _SINGLE_PATH_STATUS:
            if index + 1 >= len(tokens):
                raise VcsError(f"truncated diff record after status {status!r}")
            changes.append(ChangedFile(path=tokens[index + 1], status=_SINGLE_PATH_STATUS[code]))
            index += 2
        else:
            raise VcsError(f"unsupported diff status {status!r}")
    return tuple(changes)


def changed_files(base: str, head: str | None, cwd: Path) -> tuple[ChangedFile, ...]:
    """Diff base against head, or against the working tree when head is None.

    Renames are detected (-M). Copies count as additions of the new path,
    type changes count as modifications.
    """
    target = base if head is None else f"{base}..{head}"
    raw = _run_git_text(["diff", "--name-status", "-z", "-M", target], cwd)
    return _parse_name_status(raw)


def list_files(ref: str | None, cwd: Path) -> tuple[str, ...]:
    """List repo-relative paths at ref, or in the working tree when ref is None.

    The working tree listing includes untracked files, minus anything gitignored.
    """
    if ref is None:
        # ls-files only looks below its cwd, so anchor it at the repo root.
        root = repo_root(cwd)
        raw = _run_git_text(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
    else:
        raw = _run_git_text(["ls-tree", "-r", "--name-only", "-z", ref], cwd)
    return tuple(entry for entry in raw.split("\0") if entry)


def blob_shas(ref: str, cwd: Path) -> Mapping[str, str]:
    """Map every repo-relative path at ref to its blob sha."""
    raw = _run_git_text(["ls-tree", "-r", "-z", ref], cwd)
    shas: dict[str, str] = {}
    for entry in raw.split("\0"):
        if not entry:
            continue
        meta, tab, path = entry.partition("\t")
        fields = meta.split(" ")
        if not tab or len(fields) != 3:
            raise VcsError(f"unparseable ls-tree entry {entry!r}")
        _mode, object_type, sha = fields
        # Submodules show up as commit objects; they carry no blob content.
        if object_type == "blob":
            shas[path] = sha
    return shas


def read_blob(ref: str, path: str, cwd: Path) -> bytes:
    """Return the raw bytes of path at ref. Raises VcsError if it does not exist."""
    return _run_git(["cat-file", "-p", f"{ref}:{path}"], cwd)


def resolve_sha(ref: str, cwd: Path) -> str:
    """Resolve ref to the full sha of the commit it points at."""
    return _run_git_text(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd).strip()


def blob_sha_of_bytes(content: bytes) -> str:
    """The sha git hash-object would assign to these bytes."""
    hasher = hashlib.sha1(usedforsecurity=False)
    hasher.update(f"blob {len(content)}\x00".encode())
    hasher.update(content)
    return hasher.hexdigest()


def fingerprint_of_shas(shas: Mapping[str, str]) -> str:
    """Canonical tree fingerprint: sha256 over sorted path TAB sha lines."""
    lines = sorted(f"{path}\t{sha}" for path, sha in shas.items())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _index_shas(root: Path) -> tuple[dict[str, str], frozenset[str]]:
    """Stage-0 index blob shas by path, plus the set of gitlink paths."""
    raw = _run_git_text(["ls-files", "-s", "-z"], root)
    shas: dict[str, str] = {}
    gitlinks: set[str] = set()
    for entry in raw.split("\0"):
        if not entry:
            continue
        meta, tab, path = entry.partition("\t")
        fields = meta.split(" ")
        if not tab or len(fields) != 3:
            raise VcsError(f"unparseable ls-files -s entry {entry!r}")
        mode, sha, stage = fields
        if mode == _GITLINK_MODE:
            gitlinks.add(path)
        elif stage == "0":
            shas[path] = sha
    return shas, frozenset(gitlinks)


def _dirty_paths(root: Path) -> frozenset[str]:
    """Paths whose worktree content may differ from the index, plus untracked."""
    raw = _run_git_text(["status", "--porcelain", "-z"], root)
    tokens = raw.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    dirty: set[str] = set()
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        if len(entry) < 4:
            raise VcsError(f"unparseable status entry {entry!r}")
        status, path = entry[:2], entry[3:]
        dirty.add(path)
        # Renames and copies carry the origin path in the next token.
        index += 2 if status[0] in ("R", "C") else 1
    return frozenset(dirty)


def working_tree_fingerprint(root: Path, exclude: frozenset[str] = frozenset()) -> str:
    """Fingerprint the working tree at the repository root.

    Clean tracked files reuse their index blob shas; dirty and untracked files
    are hashed from disk. Unreadable or vanished files are omitted, and both
    the pipeline and the pytest plugin run this same code, so any divergence
    between select time and test time surfaces as a mismatch, never a skip.
    exclude names repo-relative posix paths (acquit's own output documents)
    left out on both sides, so a selection cannot invalidate itself.
    """
    raw = _run_git_text(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
    listing = tuple(entry for entry in raw.split("\0") if entry)
    index_shas, gitlinks = _index_shas(root)
    dirty = _dirty_paths(root)
    shas: dict[str, str] = {}
    for path in listing:
        if path in gitlinks or path in exclude:
            continue
        sha = index_shas.get(path) if path not in dirty else None
        if sha is None:
            try:
                sha = blob_sha_of_bytes((root / path).read_bytes())
            except OSError:
                continue
        shas[path] = sha
    return fingerprint_of_shas(shas)


def ref_tree_fingerprint(ref: str, cwd: Path) -> str:
    """Fingerprint the committed tree at ref, blobs only."""
    return fingerprint_of_shas(blob_shas(ref, cwd))
