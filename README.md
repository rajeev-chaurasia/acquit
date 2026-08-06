# Acquit

Provably skip unaffected pytest tests on every pull request. Static analysis, fail-closed, with evidence for every skip.

> Status: early alpha. The analysis engine is under active development; the CLI currently always answers "run everything", which is also its permanent failure mode.

## What it will do

Acquit builds a dependency graph of your repository from import statements alone (it never executes your code), then works out which test files a pull request cannot possibly affect. Those tests are skipped, and every skip comes with a machine-checkable witness: the test's import closure and proof that it does not intersect the changed files.

Anything Acquit cannot reason about (dynamic imports, changed conftest files, dependency bumps, data files, and a documented list of other triggers) makes it fall back to running the full suite. It can only be wrong in the safe direction.

## Design principles

- Never execute user code during analysis
- Fail closed: every error path converges on "run all tests"
- Deterministic: same tree and diff produce byte-identical reports
- Explainable: every decision names the rule or the graph path behind it

## License

Apache-2.0
