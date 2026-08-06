# The acquit replay study

The study replays historical merged PRs of real repositories and checks the
headline claim: a meaningful share of test time is provably skippable, with
zero unsafe skips. Everything needed to re-run it ships in this repository:
committed manifests, constraints, quarantine lists, and the `acquit-study`
console script. The study is itself a test: an unsafe skip fails the run.

## How one PR is replayed

For each PR in a manifest, the runner checks out the base sha, builds a venv
(`uv venv` plus `uv pip install -e . pytest`, optionally pinned by a
constraints file), and runs the full pytest suite with a deterministic
environment, capturing junit xml. It repeats that at the head sha, then runs
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

## Reproducibility contract

- Manifests are committed. They pin the repo, the PR list (numbers and
  shas), the python version, and the sha256 of the constraints file used.
  The runner refuses a constraints file whose hash does not match.
- Constraints (`study/constraints/{repo}.txt`) freeze the dependency set the
  suites run under, so a dependency release cannot change outcomes between
  study runs.
- Quarantine lists (`study/quarantine/{repo}.txt`, one normalized node id
  per line, `#` comments) name known-flaky tests excluded from the
  changed-outcome set. They never excuse a skipped new test, and every entry
  is visible in review.
- Exclusions are accounted, not hidden: a PR whose base suite will not run
  is recorded in the results dir (and, with `--record-exclusion`, in the
  manifest) with the stage that failed, and the summary reports the
  histogram.
