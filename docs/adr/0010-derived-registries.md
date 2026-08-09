# ADR 0010: derived registries do not earn a resolver

Status: proposed (recommends no build)

## Context

ADR 0009 named its measured headroom: derived registries, dicts populated
by module-level loops over literal structures, the kombu and celery
lazy-init idiom, "9 sites, two 100%-blast repos", to arrive as its own
measured proposal. This is that proposal, built the established way: an
exact grammar with a soundness argument per rule, an adversarial
counterexample hunt, and measured rates from the census corpus. Full
analysis: [design/derived-registries.md](../design/derived-registries.md).

The recount comes first, because the headroom was mislabeled at birth: the
"9 sites" was a decline-bucket population, not an idiom count. Site by
site it holds 3 lazy-init registry consumers (kombu 1, celery 2) plus 6
unrelated subscript shapes (pytest fixtures, walk_packages discovery, a
sphinx hook, a filesystem example). Of the 3, celery's two are class
attributes whose real contents arrive from another module through a
function parameter, a dict-comprehension helper, and a type() call; no
per-module grammar can prove them, and admitting class-body bindings would
fold them to the empty set, the vacuous-subset lie of ADR 0009's
accumulator counterexample in class clothes. The provable population of
the named idiom is kombu's one site.

## Decision

Specify the grammar, publish the measurement, and do not build the
resolver. Adopt the three corrections the counterexample hunt forced on
ADR 0009's rules, which are the durable output of this design.

- The grammar (specified and prototype-validated, admitted shapes and
  soundness arguments in the design doc): a module-level dict qualifies
  when its single binding is a display or foldable comprehension and every
  other mention is a whitelisted read or a derivation store whose value
  expression folds; consumption folds to the union over the display and
  every store, which quantifies over statements, not executions, so
  conditional inserts and uncalled registration helpers cost nothing. Key
  transformations are free because only values feed the importer (KeyError
  raises before any import); the same transformation on a value still
  declines under the interpreter-coupling rule. Single-binding aliases
  join the registry as one family; parameters, augmented stores, del,
  bare-argument escapes, and class attributes decline. A fromlist/key
  coupling recovers `__import__(R[name], None, None, [name])`: left-to-
  right argument evaluation bounds the fromlist entry by the registry's
  key set, and the resulting pairs resolve with from-import semantics.
- Correction one, the STATICA_HACK hole: stores through globals(), vars(),
  module-level locals(), module __dict__, or setattr on sys.modules
  entries rebind module-scope names invisibly to the binding census that
  every v1 name fold relies on, and kombu's init carries the idiom
  verbatim. A poison ladder repairs it (literal key poisons that name;
  unknown key with a non-string constant poisons only str()-coercion
  contexts; anything else poisons every module-scope read, provenance and
  anchors included). Measured: the ladder changes no published v1 fold, so
  the hole is latent, and it should land in the production folder before
  folding ships.
- Correction two: fromlist entries must resolve as from-imports (edge only
  when module.entry is an indexed module), not as absolute dotted names,
  or every attribute-shaped entry reads as broken first-party and re-taints
  the file the fold just cleaned. Harmless in v1 (no folded site carried a
  fromlist); load-bearing for any future tier that folds one.
- Correction three, the observer lesson at construction time: every fold
  that reads module-scope state assumes no other module rebinds that state
  through attribute stores, and the assumption must be checked, not held
  silently. Per-blob facts record mutating mentions of imported-module
  attributes; graph construction joins them against each fold's recorded
  dependency names. A hit taints the mutating file (the sys.modules-store
  precedent: a mutation executes only inside closures that contain the
  mutator, and cross-test leakage is A4's exclusion), rather than
  declining the fold; both semantics were measured, and the strict variant
  zeroes the tier at the hands of kombu's own test conftest. Invisible
  channels (getattr from strings, exec, laundered module objects) are the
  same accepted residue every detector carries, with the oracle's
  runtime-import-subset assertion as the tested backstop.
- The measurement, from the prototype over the census corpus (579 sites
  across 60 of the 61 clones, sympy excluded by the same RecursionError
  as every prior measurement; v1 column reproduces ADR 0009's published
  27 folds exactly): the derived grammar folds exactly one additional
  site corpus-wide, kombu's flagship init, eight names plus 120 coupled
  pairs, cap-exact at 128, taking the corpus from 27 folds and 9 fully
  de-suspected files to 28 and 10. R007 and overall taint blast move in
  no repo over the corrected v1 baseline, and the medians hold at 100
  percent. kombu, the repo the tier was named for, de-taints its fat
  init and gains nothing: all 62 of its collected test files also reach
  t/unit/conftest.py, whose six module-mocking sites no sound fold can
  bound, so everything stays pinned before and after. celery cannot fold
  by construction. The parametric residue ADR 0009 measured at 76% of
  occurrences is untouched: registries that feed the wild plugin-loader
  patterns live across module boundaries or are populated by registration
  functions, exactly what the grammar must decline.

## Consequences

- No resolver is built. A checker whose corpus-wide selection delta is
  zero does not earn permanent residence in graph/resolvers/, a schema
  bump, oracle fixtures, a mutation arm, and a cap negotiation (kombu
  lands on the 128 cap exactly). This is the same honesty ADR 0008 and
  0009 bought with their measurements, one step earlier: they shipped
  because they retired real pins; this one retires none.
- The narrowing campaign sharpens the discount rate. Re-export narrowing
  went to real merged PRs with 6.7 percent static applicability on the
  repos it targets and produced zero narrowed skips across 259 PRs
  (flask 41, rich 64, httpx 83, black 60, uvicorn 11), because a real PR
  must thread every intersecting file through every condition jointly,
  and one
  non-inert file anywhere voids the whole skip. Static site counts
  overstate real-PR yield by a large factor; that is now a measured
  precedent, not a suspicion. Applying the same discount here is easy
  arithmetic: this tier's static applicability is one repo's fat init,
  its selection delta is zero before any discount, and the joint
  condition it would have to thread (every other suspect in the closure
  also resolved) already fails today on kombu's own conftest. A design
  whose ceiling under the most optimistic accounting is zero does not
  get better under honest accounting.
- The corrections outlive the decline. The globals-store poison and the
  fromlist resolution belong in the v1 folder that just landed; the
  external-mutation join is specified and waiting if any module-state fold
  ever ships beyond v1's registry rule, and the seam's ResolveContext
  already carries the facts mapping it needs. The 44-case gallery joins
  the record so the next design starts where this one stopped.
- The alternative for kombu-shaped repos is a waiver, documented honestly:
  `[tool.acquit.waive]` on R007 for the registry module, with the
  justification that the lazily imported modules are all imported directly
  by the suite anyway (true in kombu, where the tests import
  kombu.connection and friends by name). The exact recipe such a repo
  would carry:

      [[tool.acquit.waive]]
      rule = "R007"
      glob = "kombu/__init__.py"
      justification = "Lazy-init registry over a literal table; every module it can import, the suite imports directly by name."

  The proof obligation shifts to the user, the finding stays in the
  report, and no machinery pretends a waiver is a bound. This is not a
  consolation prize; it is the lever the study already measured as the
  high-yield one: the aggregates document the analogous recipe for R001,
  where an assume_inert list recovers 30
  of black's 43 R001-blocked run-alls and lifts its counterfactual
  selective share to 51.7 percent, dwarfing anything any resolver in this
  family has measured on real PRs. A repo that wants the bound without
  the waiver can also just write the registry as a literal display; ADR
  0009's rule folds that today, and the diff is mechanical.
- What this decision does not license: cross-module registries (pygments'
  _mapping files, celery's loader tables) and interprocedural value flow
  stay out on the same grounds as ADR 0009's cross-module constants, now
  with the added evidence that the one interprocedural flagship (celery)
  requires dynamic class construction on top. What would reopen the build
  decision is named evidence, not another static count: real-PR demand
  from adopters (a replay-study stream over merged PRs where a
  registry-module taint is the sole blocker on skips that would otherwise
  land, the bar the narrowing campaign set), or a census over a corpus
  where module-local derived registries are actually common. The grammar
  here is specified, gallery-pinned, and ready to be re-measured against
  either.
