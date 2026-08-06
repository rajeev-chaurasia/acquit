# Acquit

Provably skip unaffected pytest tests on every pull request. Static analysis, fail-closed, with evidence for every skip.

> Status: alpha. The engine works: on real pull requests acquit makes selective decisions, and every skip ships with a machine-checkable witness that is re-verified by replay before pytest honors it. Every failure mode still converges on "run everything". Not yet on the GitHub Marketplace, so expect rough edges and no version pinning.

## What it does

Acquit builds a dependency graph of your repository from import statements alone (it never executes your code), then works out which test files a pull request cannot possibly affect. Those tests are skipped, and every skip comes with a machine-checkable witness: the test's import closure and proof that it does not intersect the changed files.

Anything Acquit cannot reason about (dynamic imports, changed conftest files, dependency bumps, data files, and a documented list of other triggers) makes it fall back to running the full suite. It can only be wrong in the safe direction.

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
