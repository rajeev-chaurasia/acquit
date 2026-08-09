# ADR 0008: re-export narrowing for fat package inits

Status: proposed

## Context

The census across 60 repositories found the dominant selectivity ceiling is
structural: in 55 repos, one top-level `__init__.py` sits in at least half
of all test closures (median: all of them), so any core-module change
reaches 100% of tests at file granularity. The study saw the same ceiling
as `full-graph-impact` on 5 flask, 11 rich, and 20 httpx PRs. The cause is
re-export: a test does `from pkg import Table`, the init re-exports Table
from `pkg/table.py` and also imports `pkg/console.py` for its other
re-exports, so `console.py` is genuinely executed at import time and
genuinely in the test's runtime closure. The oracle proves that, so the
closure may not shrink. What can change is impact: a change to `console.py`
affects that test only if `console.py`'s import-time behavior changed.

## Decision

Narrow impact, never the closure, through a new edge kind and a stronger
witness claim. Full analysis, whitelists, counterexamples, and measured
rates: [design/reexport-narrowing.md](../design/reexport-narrowing.md).

- A proven pure re-exporter `__init__.py` (docstring, imports,
  from-imports, literal `__all__`, literal metadata assignments,
  TYPE_CHECKING blocks; anything else disqualifies) gets its outgoing
  edges marked `INIT_REEXPORT`. Graph schema bumps to 2.
- Consumers keep full edges to the init, every prefix, and the home
  submodules of the symbols they import, resolved through chains of pure
  inits. Unattributable names, plain `import pkg` consumers (attribute
  uses are invisible), star imports, and inits with `__getattr__` all get
  the full fan-out: no narrowing, exactly today's closure semantics.
- The semantic closure is the closure minus `INIT_REEXPORT` edges. A
  changed file that is import-time-only (in the closure, outside the
  semantic closure, at base and head) may be excused from impact only when
  it is modified in place, passes an inertness whitelist at both
  revisions, and keeps its module-level bound-name set and resolved edge
  set identical across them. The relational conditions are load-bearing:
  a symbol rename inside a whitelist-inert module is an import-time
  behavior change (the init's from-import raises at head), and only a
  base-to-head comparison can reject it.
- The inertness whitelist admits only statements that bind names without
  executing user code: imports, inert assignments, defs with inert
  defaults and annotations, local-base hookless classes with constant-only
  class bodies, TYPE_CHECKING guards, whitelisted try/except. All calls
  reject, including decorators, TypeVar, namedtuple, enum and dataclass
  machinery, and imported base classes (subclass-registry hooks run code
  at class creation). Anything debatable rejects; a decline is always
  sound because it reproduces today's behavior.
- Witnesses carry a second claim, `closure(test) intersects changed set
  only in import-time-only files, each modified in place and import-inert
  across base and head`, plus the per-file evidence (blob shas, inits
  crossed). Witness schema bumps to v2. Replay rebuilds both snapshots,
  re-runs both checkers on both revisions, and recomputes both semantic
  closures; narrowing requires a real base commit, so working-tree
  selections never narrow.
- Rollout is evidence-gated: ship disabled, then canary-only (narrowed
  decisions get live outcome validation for free), then a
  mutation-injection study arm that mutates bodies and literal constants
  in import-time-only files and asserts consumers of other symbols may
  skip while consumers of the mutated file's symbols must run, then a
  replay-study re-run, and only then opt-in enforce. The mutation arm is
  required because outcome-diffing merged PRs has near-zero power against
  symbol-level unsoundness.

## Consequences

- Soundness posture is unchanged: closures never shrink, the oracle's
  invariant holds as-is, every narrowed skip has a replay-checkable
  witness, and every uncertain path declines into today's behavior.
- The measured win is honest and smaller than the census score implied.
  Prototype rates over the census corpus: 15 of 55 fat inits (27%) are
  pure re-exporters; 22% of the 4853 submodules in fat packages pass the
  strict inertness whitelist at HEAD (median repo: 14%). On the three
  study repos' actual PR history the strict design recovers zero of the
  36 full-graph-impact PRs: rich and httpx have impure inits, and flask's
  core modules construct objects at import time. The census scored
  exposure; this measured recoverability, and the gap is the finding.
- The durable value is the machinery: the `graph/resolvers/` seam
  (recognize, then prove a bound or decline), the relational claim shape,
  and the mutation arm apply to every future narrowing. Next residents
  per the census: constant-folding for non-literal dynamic imports, then
  sys.path-mutation folding; exec-eval of non-literal strings is
  unresolvable on principle and stays fail-closed.
- The measured headroom (25.4% inert with a vetted-constructor tier) says
  the follow-up worth designing is a per-constructor allowlist argued
  item by item, not a looser whitelist.
