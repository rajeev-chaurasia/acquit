# r/Python post (flair: Showcase)

Title:

Acquit: provable pytest test skipping via static import graphs, validated by replaying 198 real PRs

Body:

**What my project does**

Acquit decides which pytest test files a pull request cannot possibly affect, and skips them with evidence. It builds a dependency graph purely from parsing import statements, pytest config, and conftest scoping (it never executes your code), then computes each test file's import closure. If the closure is provably disjoint from the changed files, the skip ships with a machine-checkable witness: closure hash, changed set, disjointness claim. `acquit replay` rebuilds the graph from scratch, without any cache, and re-verifies every witness before the pytest plugin honors a selective run.

The interesting part is what happens when static analysis cannot bound something. Non-literal `importlib`, `exec`, `sys.path` mutation, changed conftests, dependency manifest bumps, unparseable files, and changed non-Python files all go through an ordered rule table (R001 to R018), and every rule converges on "run more tests", up to running everything. The default answer is always the full suite; a skip requires an affirmative proof.

To find out whether this holds outside my own test suite, I replayed 198 merged PRs from click, flask, rich, and httpx: full suite at the base commit, full suite at the head commit, in a frozen environment, then a differential check that no test whose outcome changed (or that only exists at head) lived in a skipped file. Results: 0 unsafe skips across all 198 PRs, every selective run replay-verified, and a median of 93 to 99 percent of suite time skipped when acquit is selective. Also, honestly: only 5.1 percent of PRs were selective out of the box, mostly because docs and changelog edits ride along with almost every PR and fail closed; a one-line `assume_inert` config lifts the counterfactual to 60.1 percent. The full writeup, including how the study caught one of my own fixes overshooting flask to zero percent selective, is here: https://github.com/rajeev-chaurasia/acquit/blob/main/docs/study.md

**Target audience**

Teams with slow pytest suites on GitHub Actions who are uncomfortable with probabilistic test selection. There is a report-only mode that never skips anything, just posts a PR comment explaining what it would have done and why, so you can audit the decisions for a few weeks before trusting enforce mode. Alpha status: the CLI is on PyPI (0.0.1), the Action currently runs from `@main` until the 0.1.0 Marketplace release.

**Comparison**

pytest-testmon selects at function level from runtime coverage, which is more precise, but it executes your code under a tracer and depends on per-machine coverage state; acquit is a stateless pure function of (tree, diff) that never executes the code under analysis. Codecov ATS is hosted testmon, commercial. Launchable is ML-based and probabilistic by design; a wrong skip there is a tuning parameter, here it is a bug with a failing replay. Bazel and Pants are sound but require migrating your build system. Path-filter globs are unverified guesses. Acquit sits in one spot: fail-closed, deterministic, per-skip proofs, works on an unmodified pytest project.

I would genuinely like methodological criticism of the study before I trust it further: sampling (newest-first merged PRs, 18-month window), the differential-outcome safety check, the quarantine handling for flaky tests, and especially the counterfactual column, which is computed from recorded findings rather than re-replayed with the config applied (the click mini-study did re-run its counterfactual for real; the 198-PR table does not). If you can construct a diff that produces an unsafe skip, I want the issue.

Repo: https://github.com/rajeev-chaurasia/acquit
