# ADR 0009: constant folding for non-literal dynamic imports

Status: accepted

Implemented in `graph/resolvers/folding.py` as the seam's second resident:
the v1 grammar as specified, the prototype gallery promoted to committed
tests, cache format 8, graph schema 3.

## Context

R007 taints any file that imports a module chosen at runtime, and the taint
is standing: every test whose closure reaches the file runs on every diff,
whatever changed. The census ranked this hazard second (42 of 60 repos carry
the idiom, 36 with test-reachable instances, median blast radius 100% where
present). Some of those "runtime-chosen" names are statically knowable: a
loop over a tuple of literals, a module constant assigned once, a version
switch between two literal names. Those sites can be proven to import only
modules from a finite set and given ordinary `DYNAMIC_IMPORT` edges instead
of the taint, exactly as if each name were a literal.

## Decision

Fold what can be proven, decline everything else into today's taint. Full
grammar, flow rules, counterexamples, and measured rates:
[design/constant-folding.md](../design/constant-folding.md).

- The invariant is superset-only: a fold must contain every name the site
  can pass to the import machinery, or the site declines. Extra edges are
  sound (closures only grow); a missing name is the one unsound direction.
  Where enumerability fails partially, the site declines entirely: the
  dynamic residue can import anything, so no finite edge set bounds it, and
  the sound alternative (edges plus taint) provably changes no selection
  while dressing a non-bound up as one.
- The v1 grammar, each rule with its own soundness argument: literals,
  concatenation, f-strings with bare fields, conditional and boolean
  expressions as unions; names whose every binding occurrence in their
  owning scope (all binding constructs counted, `global` writes included)
  is a string literal, folded to the union of the literals; loop variables
  whose only binding is a `for` or comprehension target over an inline
  literal display, a literal dict's keys, or a named all-literal tuple;
  module-level literal dict registries with a strict no-alias mention
  whitelist, folded to value unions under subscript and `get` (KeyError
  imports nothing, so the key needs no bound); `__package__` and `__name__`
  as anchors resolved against index identities like relative imports;
  `__import__` fromlist and level handled explicitly. Callee provenance is
  mandatory: `import_module` must trace to importlib, `__import__` must be
  unshadowed, `sys` must be the real module; the detector's name-based
  over-approximation is sound for tainting and would be unsound to fold.
- Folded names feed the existing literal-dynamic-import pipeline unchanged:
  first-party names become `DYNAMIC_IMPORT` edges with prefix edges,
  externals become external edges, and a folded name that looks first-party
  but does not resolve keeps the file tainted. No new rule; R007 simply
  stops firing on proven sites.
- Folding runs at fact extraction and is a pure function of one module's
  bytes plus its identity under the import roots. Graph schema and parse
  cache versions bump; witnesses, claims, selection documents, and replay
  are untouched, because replay re-derives every fold when it rebuilds the
  snapshot it already rebuilds. This is the cheapest resolver shape the
  seam admits, and the design treats that as a property to defend.
- The soundness oracle validates folds directly, which narrowing never got:
  a folded file stops being the documented tainted exception, so the
  oracle's runtime-import-subset assertion covers the folded site on every
  run. Fixture repos with foldable idioms (and one near-miss that must stay
  tainted) land with the checker. Rollout is otherwise the narrowing bar:
  ship disabled, canary on acquit and the study corpus, then a mutation arm
  (mutate folded targets: reaching tests must be selected; mutate non-folded
  siblings: any outcome flip in a skipped test is a folder bug; plus an
  import-log probe asserting observed imports land inside folded edges),
  then a replay-study re-run, then opt-in enforce. Probes only validate the
  arms that execute on the study machine; union arms for other platforms
  rest on the value-union argument alone, accepted explicitly.

## Consequences

- Soundness posture is unchanged: every fold is a proof or a decline, a
  decline reproduces today's behavior exactly, and the one new failure mode
  (a subset fold) is exactly what the oracle fixtures, the adversarial
  gallery, and the mutation arm are aimed at.
- The measured win is small and the ADR says so. Prototype rates over the
  census corpus: 27 of 579 occurrences fold (4.7%; 4.5% weighted to
  test-reachable sites), 8 test-reachable files shed their last dynamic
  suspect, the R007 pin disappears in 2 of 42 affected repos (pipx, nox)
  and shrinks in 1 more (fastapi), and median blast radii do not move. 76%
  of occurrences take their name from a parameter or a config attribute,
  which no sound fold can bound: the census scored exposure, this measured
  recoverability, and the gap is wider than narrowing's. The dominant
  residue is dynamic on principle, not unfinished grammar.
- Build it anyway, and keep it thin: the checker is small, single-revision, and
  oracle-checkable; it retires a real class of standing taints permanently
  rather than per-diff; and it makes `graph/resolvers/` a framework in fact
  by adding the second resident on the opposite side of the axis narrowing
  occupies (per-revision graph-construction proof versus relational
  selection-time proof), forcing the seam to name that axis before a third
  resident needs it. What this decision explicitly does not license is the
  expensive tiers: cross-module constants (zero corpus demand) and string
  method evaluation (interpreter-coupled, breaks byte-for-byte replay
  derivation) stay out.
- The measured headroom worth designing next is derived registries: dicts
  built by module-level loops over literal structures, the kombu and celery
  lazy-init idiom (9 sites, two 100%-blast repos). That is a harder escape
  argument on top of exactly the registry machinery specified here, and it
  should arrive with its own measured proposal rather than ride this one.
