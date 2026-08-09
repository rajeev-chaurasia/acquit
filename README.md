# Acquit

Provably skip unaffected pytest tests on every pull request. Static analysis, fail-closed, with evidence for every skip.

> Status: alpha. The engine works: on real pull requests acquit makes selective decisions, and every skip ships with a machine-checkable witness that is re-verified by replay before pytest honors it. Every failure mode still converges on "run everything". The CLI is on PyPI (`pip install acquit==0.0.1`), so the analysis itself pins cleanly. The GitHub Action is not yet on the Marketplace; that arrives with 0.1.0, and until then the action is used via `@main`.

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

The docs-config column is computed from the recorded findings for flask, rich, and httpx; click's docs-config counterfactual was re-replayed for real, with every additional selective decision verified (see [docs/study.md](docs/study.md)).

The out-of-the-box share is low because acquit is fail-closed: any PR it cannot fully prove safe runs the whole suite. Docs and changelog churn (rule R001) is by far the dominant blocker; the docs-config column counts PRs where it was the only one, and the sticky PR comment suggests the exact `assume_inert` entry to lift it. Every number above regenerates from committed manifests via `acquit-study`; see [study/README.md](study/README.md). The full writeup, including the parts that went wrong, is in [docs/study.md](docs/study.md).

## How it compares

| Tool | Mechanism | Needs code execution | Deterministic | Explains each skip | Works with plain pytest | Cost |
| --- | --- | --- | --- | --- | --- | --- |
| acquit | static import graph, fail-closed rules | no | yes | yes: a witness per skip, a named rule per run-all | yes | free, Apache-2.0 |
| pytest-testmon | runtime coverage database | yes | given the same database, yes; the database is per-machine state | dependency data exists, but no proof artifact per skip | yes | free |
| Codecov ATS | testmon-based, hosted | yes | same caveats as testmon | no | yes | commercial |
| Launchable | ML over test-result history | needs outcome history | no, probabilistic by design | no, it predicts | yes | commercial |
| Bazel / Pants | declared build dependency graph | no | yes | queryable graph, no per-skip artifact | no, requires build-system migration | free, plus the migration |
| dorny/paths-filter | hand-written path globs | no | yes | no, the globs are the reasoning | yes | free |

Acquit occupies one niche: fail-closed static selection with a proof per skip, on an unmodified pytest project. If you want function-level precision and are willing to maintain runtime coverage state, pytest-testmon is the honest alternative; it is precise where acquit is conservative, and stateful where acquit is a pure function of the diff.

## Try it on a PR

One complete workflow file, ready to paste:

```yaml
name: tests

on: pull_request

permissions:
  contents: read
  pull-requests: write

jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          # The base ref must exist locally; shallow clones always fall back to run-all.
          fetch-depth: 0

      - uses: rajeev-chaurasia/acquit@main
        with:
          mode: report

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      # The pytest plugin ships in the acquit package. Enforce mode silently
      # changes nothing unless acquit is importable in the environment that
      # runs pytest.
      - run: pip install acquit
      - run: pip install -e . pytest

      - run: pytest
```

Report mode never skips a test: the action analyzes the diff, verifies the evidence with a replay, posts a sticky PR comment explaining the decision, and sets outputs. Switch to `mode: enforce` once you trust what the comments say, and the pytest plugin will skip the provably unaffected files; any unrecognized `mode` value means `report`. The `pip install acquit` line (or its uv equivalent, `uv pip install acquit`) is what puts the plugin into the environment that runs pytest. Version pinning arrives with the first release; until then `@main` is the way in.

## Run it locally

The whole loop runs without CI:

```
pip install acquit                # or: uv pip install acquit
acquit select --base origin/main
ACQUIT_SELECTION_FILE=acquit-selection.json pytest
```

`acquit select --base origin/main` compares the working tree against the base ref and writes three documents: the report (`acquit-report.json`), the selection (`acquit-selection.json`), and the witnesses (`acquit-witnesses.json`). The defaults land in the current directory and work as they are, because the selection records its own outputs and they never invalidate the tree fingerprint; pointing `--report`, `--selection`, and `--witnesses` at an out-of-tree or gitignored directory keeps the checkout tidier.

From there:

- `ACQUIT_SELECTION_FILE=... pytest` verifies the selection against the tree and deselects the provably unaffected files; without the variable, pytest is untouched.
- `acquit explain tests/test_something.py --base origin/main` walks through the decision for one test file.
- `acquit replay acquit-report.json` re-verifies every witness from first principles. Replay rebuilds the analyzed commit, and a working-tree report records no head sha, so pass `--head <commit>` at select time if you want the run to be replayable.
- `acquit analyze` prints the dependency graph's health.

## Design principles

- Never execute user code during analysis
- Fail closed: every error path converges on "run all tests"
- Deterministic: same tree and diff produce byte-identical reports
- Explainable: every decision names the rule or the graph path behind it

## FAQ

**Static analysis cannot be sound in Python.** It cannot bound everything, and acquit does not try. Anything outside what static imports can bound (non-literal importlib, exec, sys.path mutation, changed conftests, dependency bumps, unparseable files, and the rest of an eighteen-rule table) fails closed to running more tests, up to everything. Soundness comes from the direction of the failure, not from the reach of the analysis. Measured: 198 replayed PRs, zero unsafe skips, every skip re-verified by `acquit replay` from first principles.

**Why file-level selection instead of test-level?** Module-level imports are the unit static analysis can bound honestly. Function-level selection would require bounding fixture resolution and parametrization behavior, and a bound I cannot defend is a skip I will not make. Function-level granularity is on the research list, not in the product.

**What about fixtures and conftest magic?** Conftest scoping is modeled directly: a changed non-root conftest forces its whole subtree to run, a changed root conftest or pytest config forces everything, collection-altering hooks and unresolvable `pytest_plugins` entries fail closed globally. Fixtures defined in a conftest ride on those edges; fixture changes inside a test's own import closure are ordinary graph reachability.

**My PRs all touch the changelog, so this will never skip anything.** That is rule R001 doing its job until you say otherwise. An `assume_inert` list under `[tool.acquit]` vouches that named docs or data files feed no test, and the sticky PR comment suggests the exact entry when docs files are the only blocker. The proof obligation for those files becomes yours; treat `.acquit.toml` diffs like CI config in review.

## Documentation

- [CLI reference](docs/cli.md): every subcommand, flag, document schema, exit code, and the action's inputs and outputs
- [Rule reference](docs/rules.md): R001 to R018, with triggers, scopes, and the exact reason texts
- [Soundness contract](docs/soundness.md): what "provably unaffected" means, its assumptions, and its limits
- [Architecture decision records](docs/adr): fail-closed policy, static analysis, rustworkx, the action, the study, granularity, tree binding
- [The replay study](docs/study.md): 198 replayed PRs, and [how to re-run it](study/README.md)
- [Contributing](CONTRIBUTING.md): dev setup, the CI gate, and the bar for soundness-critical changes
- [Security policy](SECURITY.md): what counts as a vulnerability here, and how to report one

## License

Apache-2.0
