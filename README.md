# Acquit

Provably skip unaffected pytest tests on every pull request. Static analysis, fail-closed, with evidence for every skip.

> Status: alpha. The engine works: on real pull requests acquit makes selective decisions, and every skip ships with a machine-checkable witness that is re-verified by replay before pytest honors it. Every failure mode still converges on "run everything". Not yet on the GitHub Marketplace, so expect rough edges and no version pinning.

## What it does

Acquit builds a dependency graph of your repository from import statements alone (it never executes your code), then works out which test files a pull request cannot possibly affect. Those tests are skipped, and every skip comes with a machine-checkable witness: the test's import closure and proof that it does not intersect the changed files.

Anything Acquit cannot reason about (dynamic imports, changed conftest files, dependency bumps, data files, and a documented list of other triggers) makes it fall back to running the full suite. It can only be wrong in the safe direction.

## Measured on real history

Acquit replayed 198 merged PRs from four real repositories, running each full suite at the base and head commits and checking every skip against the observed outcomes.

| Repo | Merged PRs replayed | Unsafe skips | Median suite time skipped when selective | Selective out of the box | Selective with a docs config |
| --- | --- | --- | --- | --- | --- |
| pallets/click | 10 | 0 | 99.6% | 20.0% (2/10) | 80.0% (8/10) |
| pallets/flask | 41 | 0 | - | 0.0% (0/41) | 63.4% (26/41) |
| Textualize/rich | 64 | 0 | 93.4% | 6.3% (4/64) | 64.1% (41/64) |
| encode/httpx | 83 | 0 | 97.5% | 4.8% (4/83) | 53.0% (44/83) |
| Total | 198 | 0 | - | 5.1% (10/198) | 60.1% (119/198) |

The out-of-the-box share is low because acquit is fail-closed: any PR it cannot fully prove safe runs the whole suite. Docs and changelog churn (rule R001) is by far the dominant blocker; the docs-config column counts PRs where it was the only one, and the sticky PR comment suggests the exact `assume_inert` entry to lift it. Every number above regenerates from committed manifests via `acquit-study`; see [study/README.md](study/README.md).

## Try it on a PR

```yaml
permissions:
  contents: read
  pull-requests: write

steps:
  - uses: actions/checkout@v4
    with:
      fetch-depth: 0
  - uses: rajeev-chaurasia/acquit@main
    with:
      mode: report
```

Report mode never skips a test: the action analyzes the diff, verifies the evidence with a replay, posts a sticky PR comment explaining the decision, and sets outputs. Switch to `mode: enforce` once you trust what the comments say, and the pytest plugin will skip the provably unaffected files. Version pinning arrives with the first release; until then `@main` is the way in.

## Design principles

- Never execute user code during analysis
- Fail closed: every error path converges on "run all tests"
- Deterministic: same tree and diff produce byte-identical reports
- Explainable: every decision names the rule or the graph path behind it

## License

Apache-2.0
