# ADR 0005: replay study methodology

Status: accepted

## Context

Acquit's headline claim (a meaningful share of test time is provably
skippable, with zero unsafe skips) means nothing against its own test suite.
It has to be checked against history the tool has never seen. Merged PRs
alone are not evidence: a merged PR had green CI, so "the suite passed at
head" says nothing about which tests the change actually affected. The study
needed a ground truth for "affected" that does not trust acquit, and a setup
pinned tightly enough that anyone can re-run it and get the same numbers.
The full writeup is [docs/study.md](../study.md); the operator's guide is
[study/README.md](../../study/README.md). This ADR is the methodology in one
place for reviewers of the study.

## Decision

Replay merged PRs from real repositories and compare acquit's skips against
observed outcome changes, under these rules:

- Committed manifests pin everything: the repo, the PR list with base and
  head shas, the Python version, the extra `suite_deps` the suites need just
  to collect, and the sha256 of the constraints file. The runner refuses a
  constraints file whose hash does not match the manifest.
- Frozen constraints (`study/constraints/{repo}.txt`) fix the dependency set
  the suites run under, so a dependency release cannot change outcomes
  between study runs. `suite_deps` are part of the frozen manifest: changing
  them changes every suite venv, so a change must come with re-frozen
  constraints.
- Each PR gets two full-suite runs, at the base sha and at the head sha, in
  freshly built venvs with a deterministic environment, capturing junit xml.
  Then `acquit select --base BASE --head HEAD` and `acquit replay` run from
  the current acquit installation.
- Ground truth is differential: any test whose normalized outcome differs
  between base and head, plus any test that exists only at head, was
  demonstrably affected by the PR. The safety check is mechanical: no such
  test may live in a file acquit skipped.
- Quarantine lists (`study/quarantine/{repo}.txt`) name known-flaky tests
  excluded from the changed-outcome set. Every entry is reviewable, empty
  lists are committed deliberately, and a quarantine entry never excuses a
  skipped new test.
- Exclusions are accounted, not hidden: a PR whose base suite will not run
  is recorded with the stage that failed, and the summary reports the
  histogram. Dropping a PR silently is not an option the harness offers.
- Aggregation is generated, never hand-edited, and fails: `acquit-study
  aggregate` exits nonzero if any PR recorded an unsafe skip or a skipped
  new test, so an unsafe result cannot quietly become a table row.

## Consequences

- The study is itself a test. Re-running it is `acquit-study run` over a
  committed manifest plus `acquit-study aggregate`, and every number in the
  README regenerates from the committed per-PR results.
- The methodology caught a real overshoot (the R008 scoping story in the
  writeup), which is the strongest evidence it measures the right thing.
- The counterfactual "selective with a docs config" column is arithmetic
  over recorded findings, not a re-replay, and the docs say so plainly;
  only the click mini-study ran that configuration for real.
