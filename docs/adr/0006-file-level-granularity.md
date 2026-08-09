# ADR 0006: file-level selection granularity

Status: accepted

## Context

Acquit decides per test file, not per test function. Function-level selection
skips more, and runtime-coverage tools already offer it, so choosing the
coarser unit needs a defense. The soundness contract in
[soundness.md](../soundness.md) claims a skipped test's import-time and
import-reachable behavior is identical between base and head; whatever unit
of selection is chosen has to keep that claim honest.

## Decision

The unit of selection is the test file, because the module import closure is
the bound static analysis can actually defend. A file's imports are visible
in its source; the closure over them is a complete over-approximation of the
first-party code the file can reach, and the soundness oracle checks exactly
that claim against real pytest runs.

A per-function bound has no equivalent today:

- Fixture resolution is dynamic. Which fixture a test receives depends on
  name shadowing across conftest scopes, `autouse` fixtures it never names,
  plugin-provided fixtures, and overrides applied at collection time.
  Bounding one function's behavior means bounding all of that statically,
  for the plugin ecosystem as it exists, not just for the polite subset.
- Parametrization is code. `pytest_generate_tests`, indirect parameters, and
  computed parameter lists decide at collection time which callables exist
  and what they receive. A static claim that "this function cannot be
  affected" quietly asserts none of that machinery changed behavior.

A bound that cannot be defended is a skip that cannot be witnessed, so the
honest options were file-level granularity or no tool. The cost is measured,
not hypothetical: the study's fat-init ceiling (packages that re-export
everything from `__init__.py`, putting nearly every module in every test's
closure) is exactly what file-level closures cannot recover, and it is the
strongest argument for doing the research properly.

## Consequences

- Every skip stays within what the witness actually proves; no skip rests on
  an unmodeled fixture or parametrization effect.
- Repositories with fat `__init__.py` re-exports see low selectivity, and no
  policy change can fix that at file granularity.
- Function-level selection stays on the research list. To ship, it would
  have to prove a static bound on fixture resolution and parametrization
  behavior (or an explicit, checkable contract restricting them), extend the
  runtime soundness oracle to function-level observations, and re-run the
  replay study at function granularity with zero unsafe skips. Anything
  short of that is a precision feature bought with the soundness budget.
