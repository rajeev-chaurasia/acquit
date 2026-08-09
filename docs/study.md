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

## The precision campaign

The study bought a zero and left a ceiling: fat package inits put nearly every module in nearly every test's closure, and no policy change recovers that at file granularity. After launch I went after the precision side deliberately: measure where the fail-closed rules bite hardest, design sound narrowings for the biggest offenders, and hold every narrowing to the same evidentiary bar as a skip. This chapter is that campaign, including the part where an adversarial pass broke my first narrowing five different ways.

### The census

Before building anything I measured. `acquit-study census` ran acquit's own snapshot pipeline over shallow clones of 60 OSS repositories (61 attempted; sympy's snapshot exceeds recursion depth), nothing executed, and ranked every hazard by the share of repos where it pins at least one test times the median share of the suite it pins where present. The ranking was not close. Fat-init full-graph exposure leads at 55 of 60 repos (92 percent): one top-level `__init__.py` sits in at least half of all test closures, at the median in all of them, so any core-module change reaches the entire suite at file granularity. Non-literal dynamic imports are second, test-reachable in 60 percent of repos with a median blast radius of 100 percent where present; exec and eval third at 45 percent; `sys.path` mutation fourth. The census set the build order: re-export narrowing for fat inits first, constant folding for dynamic imports second.

### Exposure is not recoverability

The census measures exposure: how much of the suite a hazard pins. It cannot measure recoverability: how much of that a sound rule can give back. Building the fixes measured recoverability, and the gap between the two numbers is the central finding of the campaign.

For re-export narrowing, the census exposure score was 0.917, the highest the metric can produce in practice. The design-stage measurement already cut that down: on the repos where narrowing can engage at all, 26.3 percent of fat-package submodules qualified under the original conditions. The adversarial fix below cut it again, to 6.7 percent. For constant folding, 60 percent of repos carry a test-reachable non-literal dynamic import, and the prototype folds 4.7 percent of the sites (27 of 579): 76 percent of occurrences take their module name from a parameter or a config attribute, which no sound fold can bound.

The pattern holds across both: the idioms that pin the most tests are dynamic on principle, not dynamic for lack of analysis effort. A census score is an upper bound on ambition, not a forecast of the win, and the honest number only appears after you design the proof and measure what qualifies.

### The narrowing design

The mechanism behind the fat-init ceiling is re-export. A test does `from pkg import Table`; the package init re-exports `Table` from `pkg/table.py` and, for its other re-exports, also imports `pkg/console.py`. Importing the package executes `console.py`, so `console.py` is genuinely in the test's runtime closure, and the soundness oracle proves exactly that, which means the closure may not shrink. What can change is impact: a change to `console.py` affects that Table-only test only if `console.py`'s import-time behavior changed.

So the design (ADR 0008) marks the outgoing edges of a proven pure re-exporter init, defines a semantic closure with those edges removed, and excuses a changed file from impact only when it is import-time-only for the test and provably import-inert. Inertness alone was never going to be enough, and one counterexample established that before a line was written: rename `helper` to `run_helper` inside an inert module the init from-imports. Both revisions pass any per-revision whitelist you can write, the module still just binds names, but importing the package at head raises ImportError for every consumer, including the test the narrow would have skipped. No single-revision check catches it. The claim had to be relational, comparing base against head: binding surface, resolved edge set, the purity of every init on the route, and the changed file outside the test's semantic closure in both graphs.

I also measured before building. Against the census corpus, 27 percent of fat inits are pure re-exporters, 22 percent of submodules pass the strict inertness whitelist, and zero of this study's own 36 full-graph-impact PRs would have been recovered: rich and httpx have impure inits, and flask constructs context variables and proxies at import time in nearly every core file. The ADR records those rates and the decision to build anyway, because the durable value is the machinery: a resolver seam where each resolver either proves a bound or declines into today's behavior, a relational claim shape, and witnesses that replay can re-derive from the recorded revisions alone. Narrowing shipped behind configuration, disabled, gated to canary-first rollout.

### The break

Then I ran an adversarial pass against the shipped implementation, the same standing process that produced the R008 story. It constructed five unsafe narrowed skips, NARROW-1 through NARROW-5: each a real two-revision repository where every condition held, the skip was narrowed, and the skipped test's outcome genuinely differed between the revisions. A constant flip in a whitelist-inert file trips an import guard in an unchanged sibling. The same flip relays silently through an import-time registry write, so nothing raises and the narrowed skip simply hides a failing test. An `__all__` listing change breaks an unchanged star importer. An import reorder flips order-dependent side effects in unchanged siblings. An impure nested init below the pure one acts on a changed value at import time.

The shared mechanism is the actual lesson. The conditions pinned what the changed file binds: its name set, its edges, its whitelist membership at both revisions. But its bound values, its `__all__` contents, and its statement order are import-time behavior too, and unchanged non-inert code inside the import-time-only region observes all three during the test's import cascade. Condition 6 checked whether the test reaches the changed file semantically. Nothing checked whether the changed file's import-time effects reach the test. I had checked one direction of a two-direction claim, and the adversary walked in through the other.

### The fix and its price

Two changes close all five findings plus a corollary (a def-body edit is harmless only until an unchanged module in the region calls the def at import time). Condition 7: every module in the test's import-time-only region that can reach the changed file, in both graphs, must itself pass the inertness whitelist at its own revision; the refusal reason is `narrowing-refused:non-inert-observer`. An inert observer can only bind names from the changed file's values; star importers never pass the whitelist, which is what makes `__all__` content structurally unobservable from a narrowed region; registry writers and import-time callers reject on the call expression. And condition 3 strengthened from bound-name equality to binding-surface equality: the literal `__all__` value must match across revisions, absent distinct from empty, refused as `narrowing-refused:all-listing-differs`. All five reproductions live on as passing regression guards in `tests/adversarial/test_narrowing_claims.py`, narrowed witnesses record the observer accounting (a count and a hash over the region files checked), and replay re-derives the region with the production judge, so a tampered region is a replay mismatch.

The fix has a measured price. On the nine census repos whose fat init actually re-exports, the qualifying share of submodules drops from 26.3 percent on inertness alone to 6.7 percent with the observer requirement; the median per-repo rate across the pure-init repos is 10 percent. Condition 7 costs roughly three quarters of the applicability the design claimed, because most fat packages contain at least one non-inert module that imports through the package and therefore observes every sibling. The alternative was five demonstrated unsafe skips, so that is the price of the region being part of the trusted surface.

### Why the process caught it first

The break cost nothing in production, and not by luck. Narrowing ships disabled; nothing narrows unless a repository opts in through configuration. The rollout bar is written into the ADR and gated on evidence: canary mode first, where every would-be-skipped test still runs and any failure among them raises an alarm naming the witness; then a mutation arm in the study; then a replay-study re-run; only then opt-in enforcement. And the adversarial pass is a standing gate for soundness-critical changes, not a one-time event.

There is a sharper point underneath, and I want it on the record: replay verifies what it is given. Replay re-derives every recorded condition from the recorded revisions with the current checker, which makes forged and tampered evidence detectable, but when the conditions themselves are insufficient, a witness that satisfies them replays cleanly. The soundness oracle cannot see the problem either: it observes imports, and the import-time-only file genuinely is imported. For narrowing-class claims the load-bearing validators are the ones that observe outcomes rather than re-check conditions: the adversarial gate before shipping, canary mode after it, and the mutation arm in the study. That ordering is not process decoration; it is the only part of the system that could have caught the silent registry variant, where nothing raises and the skip hides a failing test.

### Campaign results

The narrowing campaign is running now across five repos: the three from the replay study plus uvicorn and black, added because their measured inert rates (30 to 44 percent of submodules) are where narrowing should actually engage. Same harness, same mechanical safety check, plus the mutation arm: synthetic diffs that flip constants and replace function bodies in import-time-only files, asserting that consumers of the mutated file's symbols are selected and that no outcome-changed test hides in a skipped file. As with every other number in this writeup, the table regenerates from committed manifests, and aggregation fails on any unsafe skip.

| Repo | PRs analyzed | Excluded | Narrowed skips | Unsafe skips (all) | Replay | Mutation parity |
| --- | --- | --- | --- | --- | --- | --- |
| pallets/flask | 41 | 0 | 0 | 0 | 0/0 | not run |
| Textualize/rich | 64 | 0 | 0 | 0 | 4/4 | not run |
| encode/httpx | 83 | 0 | 0 | 0 | 4/4 | not run |
| psf/black | 60 | 0 | 0 | 0 | 1/1 | 100% (105 mutants, 58 killed by full, 0 missed) |
| encode/uvicorn | 11 | 49 (base suite red under the frozen env) | 0 | 0 | 0/0 | 0 eligible mutants |
| Total | 259 | 49 | **0** | **0** | 9/9 | 0 missed |

The number that matters most in this table is the one I did not want: zero
narrowed skips across 259 analyzed PRs, including black, the repo chosen
because its inert-module rate made it the best case. The refusal histograms
say why: on rich and httpx the changed files themselves fail inertness (423
and 566 refusals); on flask and black taint pins the candidates before
narrowing is even consulted. The static applicability number counted files
that could narrow in isolation; a real pull request narrows only when every
intersecting file passes every condition at once, and on real history that
joint event never occurred. Exposure is not recoverability, and static
recoverability is not real-PR yield. The machinery stays: it is sound,
tested, adversarially hardened, and free when it does not fire; and the
constant folder (a per-revision proof, no joint conditions) shows the same
pipeline delivering wins that do survive contact with history.

### What this says about precision work

The campaign's arithmetic runs one way: 0.917 of exposure became 26.3 percent of design-time applicability became 6.7 percent after the claim survived an adversary. Every step down bought a stronger sentence. That is what proof-based precision work looks like from the inside: every gain is bounded or declined, every bound is measured rather than estimated, and every claim is attacked before anything ships under it. The numbers get smaller at each stage, and the smaller numbers are the ones I can stand behind.

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
