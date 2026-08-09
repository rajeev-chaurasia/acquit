# Proving a test cannot be affected: 198 replayed pull requests

I built [acquit](https://github.com/rajeev-chaurasia/acquit) to answer a narrow question: can a tool skip pytest tests on a pull request and prove, for every single skip, that the change cannot affect the skipped test? This post covers the design, the replay study that checked it against real history, and where the results are less flattering than the headline.

## The problem

CI suites only grow, and once a suite is slow enough people batch changes and the feedback loop degrades from there. The existing ways out each carry a cost:

- Runtime coverage selection (pytest-testmon and the commercial services built on it) is precise, but it executes your code under a tracer and depends on a coverage database staying fresh across machines and CI runs.
- ML-based selection (Launchable and similar) predicts which tests matter. It is probabilistic by design; a wrong skip is a tuning parameter, not a bug.
- Bazel and Pants get soundness from declared dependencies, but only after you migrate your whole build.
- Path-filter globs (dorny/paths-filter and hand-rolled equivalents) are guesses that rot silently as the code moves.

None of these give a per-skip proof on a plain pytest project without executing it.

## The bet

The standard objection to static analysis here is that Python is too dynamic for it to be sound: importlib, `exec`, `sys.path` mutation, module `__getattr__`. The objection is correct about what static analysis can bound, but soundness does not require bounding everything. It requires never skipping anything you have not bounded.

Acquit parses import statements, pytest configuration, and conftest scoping; it never executes the code under analysis. The graph over-approximates on purpose: `TYPE_CHECKING` blocks, imports inside `try/except ImportError`, platform conditionals, and function-body imports are all edges. Everything the analysis cannot bound goes through a fixed rule table (R001 through R018): changed non-Python files, dependency manifests, CI and pytest config, conftest changes, non-literal dynamic imports, `exec`, unparseable files, unresolvable imports, and, as R018, any internal error. Every rule converges on the same answer: run more tests, up to running everything. Selection is a pure function of the tree and the diff, so the same inputs produce byte-identical output on any machine.

The cost of this design is precision, and the study below measures exactly how much.

## A skip is a claim with evidence

When acquit skips a test file, the claim is narrow: no changed file is reachable through the test file's import closure, so the test's import-time and import-reachable behavior is identical between base and head.

A worked example. A PR changes `src/pkg/cli.py`. The test file `tests/test_units.py` imports `pkg.units`, which imports `pkg._registry`; nothing in that closure is `pkg.cli` or anything that imports it. Acquit checks that closure against the changed set and only then constructs a witness:

```json
{
  "id": "w-000001",
  "test": "tests/test_units.py",
  "closure": "9be04a2c..." ,
  "changed": ["src/pkg/cli.py"],
  "claim": "closure(test) does not intersect changed set"
}
```

The `closure` field is the sha256 of the sorted closure listing, and the witnesses document carries the full listing keyed by that hash. The constructor refuses to build a witness whose closure intersects the changes, so a witness that should not exist cannot exist.

Witnesses would just be logs if nothing checked them. `acquit replay` rebuilds the snapshot at the recorded commit from scratch, with no cache, recomputes every skipped test's closure and hash, and re-verifies the disjointness claim from first principles. The GitHub Action runs replay before pytest may honor a selective run; a failed replay degrades the run to everything. This is also the answer to cache poisoning: a poisoned CI cache could erase import edges at selection time, but replay rebuilds without any cache and refuses the forged evidence.

## The study

A tool like this earns nothing from its own test suite; the claim only means something against history it has never seen. I replayed 198 merged PRs from four repositories: pallets/click, pallets/flask, Textualize/rich, and encode/httpx.

Merged PRs alone are not enough. A merged PR had green CI, so "the suite passed at head" says nothing about which tests the change actually affected. The harness instead runs the full suite twice per PR, at the base and head commits in a frozen environment, and diffs the outcomes. Any test whose outcome changed, or that exists only at head, was demonstrably affected by the PR, and the safety check is mechanical: no such test may live in a file acquit skipped. An unsafe skip fails the study run itself; aggregation exits nonzero.

The setup is pinned end to end. Committed manifests fix the repo, the PR list with base and head shas, the Python version, and the extra suite dependencies; a constraints file freezes the dependency set, and the runner refuses one whose hash does not match the manifest. Known-flaky tests sit in a reviewable quarantine list that never excuses a skipped new test. PRs whose base suite would not run are recorded as exclusions with the failing stage, not dropped (this sample had none). Every selective PR had its witnesses re-verified by `acquit replay`.

## Results

| Repo | Merged PRs replayed | Unsafe skips | Median suite time skipped when selective | Selective out of the box | Selective with a docs config |
| --- | --- | --- | --- | --- | --- |
| pallets/click | 10 | 0 | 99.6% | 20.0% (2/10) | 80.0% (8/10) |
| pallets/flask | 41 | 0 | - | 0.0% (0/41) | 63.4% (26/41) |
| Textualize/rich | 64 | 0 | 93.4% | 6.3% (4/64) | 64.1% (41/64) |
| encode/httpx | 83 | 0 | 97.5% | 4.8% (4/83) | 53.0% (44/83) |
| Total | 198 | 0 | - | 5.1% (10/198) | 60.1% (119/198) |

Zero unsafe skips across 198 PRs, and every selective decision passed replay. When acquit is selective it skips most of the suite: the median selective PR skipped 93 to 99 percent of suite time. Analysis costs under a second per PR on these repos.

## Warts

The number I care most about is the zero. The rest of this section is what a launch post would normally leave out.

### The R008 story

Before the study I ran an adversarial pass against my own analysis: two deliberate attempts to break it, one hunting unsafe skips, one attacking the fail-closed guarantee at the delivery layer. It found eighteen real gaps, committed as strict xfails before any fix. One of the six confirmed unsafe skips was a `sys.path` scope leak: a module-level `sys.path.insert` executed by one test's imports rewrites the import path for every later import in the process, so treating the mutation as tainting only its own closure was unsound.

The first fix was blunt: any `sys.path` mutation anywhere makes the whole run fail closed. It was safe, and the adversarial reproduction passed. Then the flask study came back zero percent selective, with R008 firing on every single PR. The trigger was a function-level `sys.path.insert` inside flask's own `cli.py`, a file reachable from essentially everywhere, so every PR was globally blocked by code it never touched.

The study caught what my reasoning had missed: the sound rule and the useful rule differ by when the mutation executes. The refined semantics ship today: an import-time mutation in a conftest is global, because conftests execute unconditionally during collection; an import-time mutation in a plain module is global only once some test can reach the module through the head graph; a function-level mutation is a closure taint, because collection-time imports are already complete when test bodies run, and a module acquired at runtime through a mutated path is dynamic loading, which carries its own taint wherever it occurs. The original adversarial reproduction still passes unchanged. This loop, adversary finds a leak, fix overshoots, study exposes the overshoot, semantics get sharper, is the reason the study exists.

### Low out-of-the-box selectivity is the contract

5.1 percent of PRs selective with no configuration is a small number, and it is the design working as intended. Fail-closed means the burden of proof sits entirely on skipping, so every PR carrying anything acquit cannot bound runs everything, loudly, with the rule named in the report. The reason histogram over the run-all PRs is the roadmap: the dominant blocker everywhere is R001, docs and changelog files riding along with the change, which is why one config entry moves the numbers so much.

### The fat-init ceiling

Some run-alls have no rule to blame: on 5 flask, 11 rich, and 20 httpx PRs the diff genuinely reaches every test file through the import graph, because these packages re-export their core modules from `__init__.py`, so every test's closure contains nearly every module. No policy change can recover these. It is the honest ceiling of file-level import granularity, and the strongest argument for the function-level research below.

### The docs-config column is arithmetic, not replay

The "selective with a docs config" column counts PRs whose only global blocker was R001, computed from the recorded findings; those PRs were not re-replayed with an `assume_inert` configuration applied. I trust the counterfactual because the click mini-study ran it for real, re-running 30 commits with the config and replay-verifying every additional selective decision, but on the 198-PR table it is arithmetic over findings, and you should read it that way. `assume_inert` also shifts the proof obligation: it is you vouching that no test reads those files, and acquit takes your word for it.

## What is next

- Function-level granularity is a research problem I want to attempt honestly or not at all: it means bounding fixture and parametrization behavior, and the fat-init ceiling is the payoff if it works.
- The analysis core sits behind a language-plugin seam; Python plus pytest is the first instance.

## Reproducing every number

Everything ships in the repository: committed manifests, constraints with pinned hashes, quarantine lists, per-PR result files, and the `acquit-study` console script. Summaries are generated, never hand-edited, and aggregation fails on any unsafe skip.

```
uv run acquit-study run --manifest study/manifests/flask.json \
  --workdir .study-work --results-dir study/results/flask --pr 5432

uv run acquit-study aggregate --results-dir study/results/flask \
  --out study/results/flask-summary.md
```

See [study/README.md](../study/README.md) for sampling new manifests, sharding, and the Actions workflow that ran the full study.

## Try it in report mode

Report mode never skips a test. It analyzes the diff, replays the evidence, and posts a sticky PR comment explaining every decision. If the comments look right for a few weeks, `mode: enforce` turns them into skips.

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

If you can construct a diff that produces an unsafe skip, that is a bug and I want it: file an issue with the diff.
