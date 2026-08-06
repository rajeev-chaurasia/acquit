# What "provably unaffected" means

Acquit's claim for every skipped test file is narrow and precise:

> Given the assumptions below, no file changed by this diff is reachable
> through the test file's import closure, so the test's import-time and
> import-reachable behavior is identical between base and head.

Every skip ships with a witness: the closure hash, the changed set, and the
disjointness claim. `acquit replay` rebuilds the graph at the recorded commit
and re-verifies every witness from first principles. A witness that cannot be
verified is a bug in acquit, not a judgment call.

## Assumptions

- A1. First-party code acquires modules through static import statements or
  literal dynamic imports. Everything else we can detect (non-literal
  importlib, exec, sys.path and sys.modules games, opaque module __getattr__,
  unparseable files) fails closed through the ADR 0001 rule table. Detection
  completeness is a tested claim, not an axiom: the soundness oracle runs real
  pytest under an import recorder and asserts every runtime-observed
  first-party import edge is inside the static closure.
- A2. The execution environment (interpreter, installed packages, OS) only
  changes in ways visible in the diff. Manifest and lockfile changes fail
  closed (R002); third-party code is otherwise treated as constant.
- A3. Files a test reads at runtime live in the repository. Any changed
  non-Python file fails closed (R001) unless the user explicitly vouches for
  it with a justified waiver or assume_inert glob, at which point the proof
  obligation is theirs.
- A4. Tests are independent: no test's outcome depends on side effects of
  another test having run. This is the one assumption acquit cannot check.
  Skipping test A can, in principle, change coupled test B's outcome. Suites
  that violate A4 are already at the mercy of pytest ordering and xdist.

## What this is not

- The graph over-approximates on purpose: conditional imports, TYPE_CHECKING
  blocks, and every branch of platform conditionals are all edges. False
  positives (running an unaffected test) are expected and harmless.
- Acquit proves impact, not determinism. A flaky test stays flaky; a skipped
  flaky test would have been flaky at base too.
- Selection granularity is the test file. Function-level selection would
  require bounding fixture and parametrization behavior that static analysis
  cannot honestly bound.
