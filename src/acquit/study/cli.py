"""Command line entry point for the replay-study harness.

Unlike the acquit CLI, this tool is allowed to fail loudly: the study is a
test, and an unsafe skip must break the build that runs it.
"""

import argparse
import os
import sys
from pathlib import Path

from acquit.errors import AcquitError, ExitCode
from acquit.gh.comment import UrllibOpener
from acquit.study.aggregate import run_aggregate
from acquit.study.census import run_census
from acquit.study.compare import parse_quarantine
from acquit.study.manifest import (
    Manifest,
    parse_shard,
    sample_pulls,
    sha256_of_file,
    write_manifest,
)
from acquit.study.runner import RunSettings, run_study


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acquit-study")
    subcommands = parser.add_subparsers(dest="command", required=True)

    sample = subcommands.add_parser(
        "sample", help="write a manifest of merged PRs, newest first, without running anything"
    )
    sample.add_argument("--repo", required=True, help="owner/name on github.com")
    sample.add_argument("--count", type=int, default=100, help="how many merged PRs to record")
    sample.add_argument("--window-months", type=int, default=18, help="recency window in months")
    sample.add_argument("--out", required=True, help="manifest output path")
    sample.add_argument("--python-version", default="3.12", help="interpreter for suite venvs")
    sample.add_argument("--constraints", help="constraints file to fingerprint into the manifest")
    sample.add_argument(
        "--suite-dep",
        action="append",
        dest="suite_deps",
        metavar="SPEC",
        help="extra pip requirement the suite needs to collect; repeatable",
    )

    run = subcommands.add_parser("run", help="replay manifest PRs and record per-PR results")
    run.add_argument("--manifest", required=True, help="manifest json path")
    run.add_argument("--workdir", required=True, help="scratch dir for mirror, worktrees, venvs")
    run.add_argument("--pr", type=int, help="replay a single PR number from the manifest")
    run.add_argument("--shard", default="1/1", help="K/N slice of the manifest, one-based")
    run.add_argument("--constraints", help="pip constraints file for the built venvs")
    run.add_argument("--results-dir", default="study-results", help="where per-PR json lands")
    run.add_argument("--quarantine", help="known-flaky node ids, one per line, # comments")
    run.add_argument(
        "--record-exclusion",
        action="store_true",
        help="append run failures to the manifest excluded list",
    )
    run.add_argument(
        "--mutants",
        type=int,
        default=0,
        help="inject up to N first-order mutants per PR into its changed .py files and "
        "check the selected set kills what the full suite kills (0 = off)",
    )
    run.add_argument(
        "--narrowing",
        action="store_true",
        help="run every select with re-export narrowing (ADR 0008) enabled by injecting "
        "narrowing = true into the head worktree's acquit config for the PR's duration",
    )

    aggregate = subcommands.add_parser("aggregate", help="fold per-PR results into a summary")
    aggregate.add_argument("--results-dir", required=True, help="tree containing result files")
    aggregate.add_argument(
        "--out", required=True, help="markdown summary path; the json lands beside it"
    )

    census = subcommands.add_parser(
        "census", help="inventory standing dynamic-idiom hazards across many repositories"
    )
    census.add_argument(
        "--repos", required=True, help="text file of owner/name slugs, one per line, # comments"
    )
    census.add_argument("--workdir", required=True, help="scratch dir for the shallow clones")
    census.add_argument(
        "--out", required=True, help="output dir: per-repo json under results/, summaries beside it"
    )
    return parser


def _run_sample(args: argparse.Namespace) -> int:
    slug = str(args.repo).strip()
    result = sample_pulls(slug, args.count, args.window_months, UrllibOpener(), dict(os.environ))
    for note in result.notes:
        print(f"study: {note}", file=sys.stderr)
    constraints_sha = None
    if args.constraints is not None:
        constraints_sha = sha256_of_file(Path(args.constraints))
    manifest = Manifest(
        repo_url=f"https://github.com/{slug}.git",
        repo_slug=slug,
        python_version=str(args.python_version),
        constraints_sha256=constraints_sha,
        window_months=args.window_months,
        prs=result.prs,
        excluded=(),
        suite_deps=tuple(str(dep) for dep in args.suite_deps or ()),
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(out, manifest)
    print(f"study: wrote {len(result.prs)} prs to {out}")
    return ExitCode.OK


def _run_run(args: argparse.Namespace) -> int:
    quarantine = frozenset[str]()
    if args.quarantine is not None:
        quarantine_path = Path(args.quarantine)
        if not quarantine_path.is_file():
            raise AcquitError(f"quarantine file not found: {quarantine_path}")
        quarantine = parse_quarantine(quarantine_path.read_text(encoding="utf-8"))
    constraints: Path | None = None
    if args.constraints is not None:
        # Installs run from other working directories, so resolve it now.
        constraints = Path(args.constraints).resolve()
        if not constraints.is_file():
            raise AcquitError(f"constraints file not found: {constraints}")
    settings = RunSettings(
        manifest_path=Path(args.manifest).resolve(),
        workdir=Path(args.workdir).resolve(),
        results_dir=Path(args.results_dir).resolve(),
        constraints=constraints,
        quarantine=quarantine,
        only_pr=args.pr,
        shard=parse_shard(args.shard),
        record_exclusions=bool(args.record_exclusion),
        mutants=max(int(args.mutants), 0),
        narrowing=bool(args.narrowing),
    )
    return run_study(settings)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "sample":
            return _run_sample(args)
        if args.command == "run":
            return _run_run(args)
        if args.command == "census":
            return run_census(Path(args.repos), Path(args.workdir).resolve(), Path(args.out))
        return run_aggregate(Path(args.results_dir), Path(args.out))
    except AcquitError as error:
        print(f"acquit-study: {error}", file=sys.stderr)
        return ExitCode.USAGE


if __name__ == "__main__":
    raise SystemExit(main())
