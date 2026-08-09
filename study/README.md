# The acquit replay study

The study replays historical merged PRs of real repositories and checks the
headline claim: a meaningful share of test time is provably skippable, with
zero unsafe skips. Everything needed to re-run it ships in this repository:
committed manifests, constraints, quarantine lists, and the `acquit-study`
console script. The study is itself a test: an unsafe skip fails the run.

## How one PR is replayed

For each PR in a manifest, the runner checks out the base sha, builds a venv
(`uv venv` plus `uv pip install -e . pytest` plus the manifest's
`suite_deps`, optionally pinned by a constraints file), and runs the full
pytest suite with a deterministic environment, capturing junit xml. It repeats that at the head sha, then runs
`acquit select --base BASE --head HEAD` and `acquit replay` from the current
acquit installation, capturing the report, selection, and witness documents.
Outcomes are normalized (parametrize ids stripped, class chains kept) and
compared: any test whose outcome changed, or that only exists at head, must
not live in a file acquit skipped.

## Sample a manifest

```
uv run acquit-study sample --repo pallets/flask --count 100 --window-months 18 \
  --out study/manifests/flask.json
```

Set GITHUB_TOKEN (or GH_TOKEN) to avoid API rate limits. Sampling lists
merged PRs newest-first, skips PRs with more than 2000 changed files, and
never runs anything. All timestamps come from API payloads, so re-sampling
against unchanged history is stable.

Repeat `--suite-dep` for every extra pip requirement the target's suite
needs just to collect (for example `--suite-dep "pytest<9"` for a repo
whose tests import a private API removed in pytest 9, or `--suite-dep trio`
for a pyproject whose filterwarnings reference it). The deps are recorded
in the manifest as `suite_deps` and installed into every suite venv.

## Run one PR locally

```
uv run acquit-study run --manifest study/manifests/flask.json \
  --workdir .study-work --results-dir study/results/flask --pr 5432
```

Add `--record-exclusion` to append run failures (base suite will not run,
environment will not build) to the manifest's excluded list. Existing
results are never re-run; delete the per-PR json to redo one.

## Run a shard

```
uv run acquit-study run --manifest study/manifests/flask.json \
  --workdir .study-work --shard 3/20 \
  --constraints study/constraints/flask.txt --results-dir results-shard
```

Shards split the manifest round-robin, so any partition of 1..N covers every
PR exactly once and each shard sees a mix of old and new PRs.

## Run the whole study on Actions

Trigger the `study` workflow (workflow_dispatch) with a repo name and a
shard count. Each shard job replays its slice and uploads its results dir as
an artifact; the final job downloads everything, runs
`acquit-study aggregate`, and uploads the summary markdown and json.

## Aggregate

```
uv run acquit-study aggregate --results-dir study/results/flask \
  --out study/results/flask-summary.md
```

Writes the markdown summary and a machine-readable json next to it. The
study table in the summary (and anything quoted in the README) is generated
from that json by this command and is never hand-edited. Aggregate exits
nonzero if any PR recorded an unsafe skip or a skipped new test.

## Mutation arm

Outcome diffing alone has near-zero power against a subtly wrong skip:
merged PRs had green CI, so the changed-outcome set is almost always empty
and a selection that wrongly skipped a consumer looks identical to a correct
one. The mutation arm manufactures the missing signal. With `--mutants N`,
after the normal replay of each PR the runner injects up to N first-order
mutants into the PR's changed .py files (comparison and arithmetic operator
flips, integer boundary tweaks, boolean flips, negation of boolean-shaped
return values, string-constant tweaks), enumerated deterministically, capped
per file, evenly spaced through the enumeration, and drawn round-robin
across files. Each mutant then gets two runs in the existing head venv: the
acquit-selected set (the already-computed head selection applied through the
real pytest plugin via `ACQUIT_SELECTION_FILE`, re-bound to the mutated
tree; deselection does the narrowing) and the full suite with `-x`. A mutant
the full suite kills must also be killed by the selected set. Detection
parity per PR is the share of full-suite kills the selected set caught, 1.0
when the full suite killed none, and any mutant killed by full but missed by
selected is listed in the summary by PR, file, and location and fails
`acquit-study aggregate`, same as an unsafe skip.

```
uv run acquit-study run --manifest study/manifests/flask.json \
  --workdir .study-work --results-dir study/results/flask \
  --pr 5432 --mutants 6
```

Mutants multiply suite runs: each one costs a selected-set run plus a capped
full-suite run (10 minutes each at most), so `--mutants` on a full manifest
roughly multiplies the study's cost by N+1. Use it on subsamples: a single
shard, a handful of PRs via `--pr`, or a small N. PRs whose head suite is
not green record the arm as skipped, because a pre-existing failure would
count as a kill for every mutant, and per-mutant apply or run failures are
recorded and skipped, never fatal to the PR. Results without mutant data
render as "not run" in the summary.

## Narrowing arm

Re-export narrowing (ADR 0008) ships disabled by default and study targets
carry no acquit configuration of their own, so `--narrowing` supplies it:
before each select the runner enables `narrowing = true` in the head
worktree's acquit config, then removes it once the PR completes. A repo with
no config gets a minimal `.acquit.toml` containing only that key; a repo
with an `.acquit.toml` or a `[tool.acquit]` section at that sha gets the key
merged in, only if absent, without clobbering roots or waivers, and the
original file is restored afterward. This is sound because select reads
acquit config from the checkout's filesystem while both analyzed snapshots
read blobs of the base and head shas, so the injected file never enters an
analyzed tree, and the base worktree never receives it. Each per-PR result
records `"narrowing": true`, the count of narrowed skips, and the
refusal-reason histogram, so aggregates can distinguish arms; the summary
gains a Narrowing section when any PR ran with the flag. Narrowed skips face
the identical unsafe-skip bar as every other skip: a changed-outcome test or
a brand-new test inside a narrowed skipped file fails the run just the same,
and the summary merely reports narrowed skips in their own column. When
`--mutants N` runs alongside, mutants for files excused by narrowed witness
blocks are restricted to function-body and constant mutations, the ADR's
protocol for import-time-only files, while every other changed file keeps
the full enumeration; a mutant only the full suite kills stays fatal, which
is exactly the ADR's directional check that consumers of a mutated narrowed
file must still run.

```
uv run acquit-study run --manifest study/manifests/flask.json \
  --workdir .study-work --results-dir study/results/flask-narrowing \
  --narrowing --mutants 6
```

## Results

Per-repo summaries live in `study/results/*-summary.md`, each generated from
the per-PR result files committed next to it: click, flask, rich, and httpx
so far. The json beside each summary carries the same numbers for machines,
and the README table quotes those json files.

Results come in two arms per repo: `study/results/{repo}/` holds the
original arm, run without narrowing, and `study/results/{repo}-narrowing/`
holds the same manifest replayed with `--narrowing`, aggregated to its own
`{repo}-narrowing-summary.md` beside it. Black joined the study during the
narrowing campaign and has no original arm, but its results keep the
`-narrowing` suffix so the directory name always states the arm. The arms
differ only in the flag: narrowed skips face the identical unsafe-skip bar
as every other skip, and `acquit-study aggregate` fails either arm the same
way on an unsafe skip or a skipped new test.

## Reproducibility contract

- Manifests are committed. They pin the repo, the PR list (numbers and
  shas), the python version, the extra `suite_deps` the suites need to
  collect, and the sha256 of the constraints file used. The runner refuses
  a constraints file whose hash does not match. `suite_deps` are part of
  the frozen manifest: changing them changes what every suite venv
  installs, so a change must come with re-frozen constraints.
- Constraints (`study/constraints/{repo}.txt`) freeze the dependency set the
  suites run under, so a dependency release cannot change outcomes between
  study runs. Click has no constraints file: its debug-scale study ran
  unconstrained by design.
- Quarantine lists (`study/quarantine/{repo}.txt`, one normalized node id
  per line, `#` comments) name known-flaky tests excluded from the
  changed-outcome set. They never excuse a skipped new test, and every entry
  is visible in review. Empty quarantine files are committed deliberately,
  so the absence of quarantined tests is distinguishable from the omission
  of a list.
- Exclusions are accounted, not hidden: a PR whose base suite will not run
  is recorded in the results dir (and, with `--record-exclusion`, in the
  manifest) with the stage that failed, and the summary reports the
  histogram.
