# What "provably unaffected" means

Acquit's claim for every skipped test file is narrow and precise:

> Given the assumptions below, no file changed by this diff is reachable
> through the test file's import closure, so the test's import-time and
> import-reachable behavior is identical between base and head.

Every skip ships with a witness: the closure hash, the changed set, and the
disjointness claim. `acquit replay` rebuilds the graph at the recorded commit
and re-verifies every witness from first principles. A witness that cannot be
verified is a bug in acquit, not a judgment call.

## The narrowed claim (ADR 0008)

With re-export narrowing enabled (`narrowing = true`; it ships disabled), a
skipped test's witness may instead carry a second, stronger claim:

> closure(test) intersects changed set only in import-time-only files, each
> modified in place and import-inert across base and head

Import-time-only means the file sits in the closure but outside the semantic
closure (the closure with the pure re-exporter inits' edges removed) at base
and at head: every route from the test to the file crosses a proven pure
`__init__.py`. Each intersecting file must additionally be modified in place,
pass the import-inertness whitelist at both revisions, and keep its binding
surface (module-level bound-name set plus literal `__all__` content) and
resolved outgoing edge set identical across them; every init the narrowing
relies on must prove pure at both revisions with the same tier; and every
module in the test's import-time-only region that can reach the file must
itself pass the inertness whitelist at both revisions, because bound values,
`__all__` listings, and statement order are import-time behavior that a
non-inert observer in the region can convert into effects reaching the test.
The witness records the per-file evidence (base and head blob shas, the
inits crossed, the observer-region accounting), and `acquit replay` rebuilds
both commits cache-free and re-derives every condition with the production
checkers before the skip is honored. Closures never shrink; a failed
condition, a tainted closure, a changed init, an added or renamed or deleted
file, or a working-tree run all decline into the disjointness behavior
above.

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

## Known limitations

- The head commit vouches for itself through acquit's own configuration.
  assume_inert globs and waivers are read from .acquit.toml (or
  [tool.acquit] in pyproject.toml) at head, so a PR can add a waiver or an
  assume_inert entry in the same diff as the change it excuses, and acquit
  honors it. Reviewers should treat .acquit.toml diffs as security-relevant,
  the same way they treat CI workflow changes.
- A runtime (function-level) sys.path mutation is scoped to its own module's
  closure, so it could in principle redirect a later function-level static
  import in another module during the same test session. This residual risk
  is accepted: collection-time imports are already complete when tests run,
  and runtime module acquisition through mutated paths is dynamic-loading
  behavior that carries its own taint wherever it occurs.
- The parse cache lives outside the checkout (ACQUIT_CACHE_DIR when set,
  otherwise the platform user cache directory, namespaced per repository
  root), so a hostile working tree cannot preseed it. CI cache restore keys
  (actions/cache and friends) remain a trust boundary: a poisoned restored
  cache can erase import edges at selection time. Replay is the backstop; it
  rebuilds the graph without any cache and refuses forged witnesses, which is
  why the shipped action replays the evidence before a selective run is
  honored.

## What this is not

- The graph over-approximates on purpose: conditional imports, TYPE_CHECKING
  blocks, and every branch of platform conditionals are all edges. False
  positives (running an unaffected test) are expected and harmless.
- Acquit proves impact, not determinism. A flaky test stays flaky; a skipped
  flaky test would have been flaky at base too.
- Selection granularity is the test file. Function-level selection would
  require bounding fixture and parametrization behavior that static analysis
  cannot honestly bound.
