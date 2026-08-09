# Re-export narrowing: analysis and evidence

Supporting analysis for [ADR 0008](../adr/0008-reexport-narrowing.md). The
ADR records the decision; this document carries the full whitelists, the
counterexamples attempted against them, the interaction analysis, and the
measured rates from the census corpus that bound the expected win.

## The problem, precisely

`tests/test_table.py` does `from pkg import Table`. `pkg/__init__.py`
re-exports `Table` from `pkg/table.py` and, for its other re-exports, also
imports `pkg/console.py`. Importing `pkg` executes `console.py` at import
time, so `console.py` is genuinely inside the test's runtime closure; the
soundness oracle proves exactly that. File-level impact therefore selects
`test_table.py` whenever `console.py` changes, even though the test touches
no console symbol. The census found this shape in 55 of 60 analyzed
repositories: one top-level `__init__.py` sits in at least half of all test
closures, and at the median it sits in all of them, so any core-module
change reaches 100% of tests.

The narrowing has to express a distinction the current graph cannot:
`console.py` is in this test's closure for import-time reasons only, and a
change to `console.py` can affect this test only if `console.py`'s
import-time behavior changed.

## The shape of the fix: weaken impact, never the closure

The closure claim is load-bearing and tested: the oracle asserts every
runtime-observed first-party import lands inside the static closure.
`console.py` is imported at runtime, so removing it from the closure would
be a lie the oracle immediately catches. The closure therefore keeps every
edge. What changes is the impact rule: a new edge kind marks which part of
a closure is import-time-only, and a changed file inside that part may be
excused from impact only when an inertness proof holds at both revisions.
Every condition below fails closed; the resolver declining is always sound
because declining reproduces today's behavior exactly.

## The edge model

- New edge kind `INIT_REEXPORT` ("init-reexport"). Only the outgoing
  from-import and import edges of a proven pure re-exporter `__init__.py`
  carry it; every other edge kind is untouched. Edge kinds are hashed, so
  this bumps `GRAPH_SCHEMA_VERSION` to 2 and R017 invalidates old caches.
- The closure of a test is computed over all edges, exactly as today. It
  never shrinks.
- The semantic closure of a test is the closure computed with
  `INIT_REEXPORT` edges removed. A file is import-time-only for a test when
  it is in the closure but not in the semantic closure, which is precisely
  "every dependency path from the test to the file crosses a pure init's
  re-export edge".
- Impact: a changed file impacts a test if it is in the test's semantic
  closure (at head, or at base for modifications and deletions, same union
  as today), or if it is in the full closure and any narrowing condition
  below fails. Selection still runs the witness constructor as an
  independent re-verification, and replay re-derives everything from
  scratch.

Consumers keep full (non-narrowed) edges to the init itself, to every
prefix init on the dotted path, and to the home submodules of the symbols
they import. Only non-home siblings become reachable solely through
`INIT_REEXPORT` edges. A change to the init or to a symbol's home selects
every consumer, exactly as today.

## The claim

The existing witness claim is untouched for disjoint closures:

    closure(test) does not intersect changed set

A narrowed witness carries a new claim:

    closure(test) intersects changed set only in import-time-only files,
    each modified in place and import-inert across base and head

where "import-inert across base and head" is the conjunction, per
intersecting file:

1. The file's diff status is modified in place. Added, deleted, and renamed
   files never narrow; there is no pair of revisions to compare.
2. The file passes the inertness whitelist at base and at head.
3. The file's module-level bound-name set is identical at base and head.
4. The file's resolved outgoing edge set (imports and literal dynamic
   imports, resolved against each revision's index) is identical at base
   and head.
5. Every `__init__.py` whose re-export edges the test-to-file paths cross
   passes the pure re-exporter check at base and at head.
6. The file is outside the test's semantic closure in both the base and
   head graphs.

Conditions 3 and 4 are relational: they compare revisions, not a single
tree. The counterexample gallery below shows why per-revision checking is
insufficient no matter how strict the whitelist is.

The witnesses document gains a `narrowed` section per witness listing each
intersecting file with its base and head blob shas, the init paths crossed,
and the checker version. Witness schema becomes `acquit/witnesses-v2`;
documents with an empty `narrowed` section are byte-compatible with v1
semantics. Narrowing requires a real base commit: working-tree selections
(no base sha to rebuild) never narrow.

### What replay must re-verify

Replay today rebuilds the head snapshot with no cache and re-verifies every
witness from first principles. For a narrowed witness it must additionally:

- rebuild the base snapshot at the recorded base sha, also with no cache;
- recompute the closure intersection with the changed set and require it to
  equal the recorded `narrowed` list exactly;
- re-run the pure re-exporter check on every recorded init at both
  revisions, from the blobs, with replay's own checker;
- re-run the inertness whitelist on every intersecting file at both
  revisions, and recompute conditions 3 and 4 from both trees;
- recompute the semantic closure in both graphs and require the file
  outside it in both.

Replay uses its own current checker, not the recorded one. If the checker
has since become stricter and now rejects a file, the witness fails replay,
which is correct: evidence that can no longer be verified is not evidence.
The recorded checker version exists for diagnostics, not trust.

## The pure re-exporter check

Applied to an `__init__.py` before any of its edges may carry
`INIT_REEXPORT`. Statement whitelist, everything else disqualifies the
whole init:

- a docstring or any bare string constant expression;
- `from X import name [as alias]`, any level, first-party or external,
  no star;
- `import X [as alias]`;
- `from __future__ import ...`;
- `__all__ = [...]` or `(...)` of string literals only;
- `NAME = <constant literal>` (version strings and similar metadata), with
  plain name targets; annotated form allowed under the same restriction;
- `if TYPE_CHECKING:` with no else branch; the guarded body never executes
  at runtime and is excluded from attribution (the main graph still
  collects its imports as edges, unchanged);
- `pass`.

Disqualifiers, explicitly: any call expression, any def or class (the init
defines behavior, not a manifest), any conditional import outside a
TYPE_CHECKING guard (including try/except ImportError: the bound-name set
becomes environment-dependent), star imports, `__getattr__` in either form
(the def form is bounded elsewhere but makes the binding set dynamic; the
assignment form is already an R012 suspect), `del`, loops, `with`,
augmented assignment, non-literal `__all__`.

A measured refinement worth its complexity: `from .mod import *` is
admissible when the star source resolves to exactly one first-party file
that has a single literal `__all__` assignment and no other module-level
mention of `__all__`. CPython binds exactly the `__all__` names in that
case, so attribution stays static. The prototype measured this tier
separately; it admits one additional repo (more-itertools) out of 55 and
notably does not admit httpx, whose init also carries a try/except
ImportError fallback def. Shadowing across multiple star sources (two
`__all__` lists exporting the same name) resolves last-writer-wins with a
fail-closed union: the consumer gets full edges to every candidate home.

## Symbol-home attribution

For `from pkg import S` through a pure init:

- If the init binds `S` via `from .sub import S`, the home is followed
  through chains of pure inits to the ultimate binding module, with a cycle
  guard. Every init on the chain gets a full edge; the final home gets a
  full edge. If any init on the chain is impure the chase stops there:
  that init's own edges are all full, so transitivity covers everything
  below it and the partial attribution is still sound.
- If the init does not bind `S` but `pkg/S.py` (or `pkg/S/__init__.py`)
  exists, the home is the submodule, matching the interpreter's fallback.
- If both exist, both get full edges (fail-closed union).
- If neither exists, the name is unattributable. Fail closed: the consumer
  gets full edges to every re-export target of the init, reproducing
  today's transitive behavior exactly. This matters: the current resolver
  relies on "the edge to the base module covers re-exports transitively",
  and narrowing invalidates that assumption unless unattributable names
  fan out fully.

Consumers that see no narrowing at all, each with the reason:

- `import pkg` followed by attribute use: attribute uses are invisible to
  the parser, so a plain-import consumer gets full edges to every re-export
  target of the init (the same fan-out, with full kinds). Closure unchanged,
  semantic closure equals closure, no narrowing.
- `from pkg import *`: same full fan-out, via the existing star expansion.
- Literal `sys.modules["pkg"]` access and literal dynamic imports of `pkg`:
  treated as plain imports, full fan-out.
- Any init with `__getattr__` or any other disqualifier: not pure, no
  `INIT_REEXPORT` edges anywhere, today's behavior.

A conftest that imports the package plainly gives every test in its scope
the full fan-out through the conftest edge. This is correct and it is a
real cost: suites whose root conftest does `import pkg` see no narrowing
until that import is removed or made symbol-specific.

## The inertness check

The question the whitelist answers: can this module's import-time execution
do anything other than bind names in its own namespace? "Bind names"
includes triggering its imports, which is why conditions 3 and 4 of the
claim pin those down relationally.

Statement whitelist at module level:

- docstring / bare constant expression; `pass`;
- `import X`; `from X import name [as alias]`; no star imports (star
  evaluates `__all__` on the source, and a missing `__all__` walks the
  module dict; either way the bound set is not visible in this file);
- `from __future__ import annotations` (and other future imports), which
  flips every annotation in the file to a non-evaluated string;
- assignment `NAME = expr` where every target is a plain name and `expr`
  is inert (grammar below); tuple unpacking only from literal displays
  (unpacking anything else invokes user iteration protocols);
- annotated assignment under the same rules, with the annotation itself
  inert unless future annotations are active;
- `def` and `async def` with no decorators, all default parameter values
  inert (defaults evaluate at import time), and all annotations inert
  unless future annotations are active; the body is unconstrained, it does
  not execute at import;
- `class` definitions under the class rules below;
- `if TYPE_CHECKING:` guards, where the guard is the literal name
  `TYPE_CHECKING`, a name bound once by `from typing import TYPE_CHECKING`,
  or `X.TYPE_CHECKING` where `X` is bound once by `import typing [as X]`
  and never rebound; the body is skipped at runtime; an else branch is
  admitted only if its statements are themselves whitelisted, because the
  else branch is what actually runs;
- `try`/`except` where the body, handlers, else, and finally blocks contain
  only whitelisted statements and every handler type is a plain name or a
  tuple of names. This admits the `try: import fast except ImportError:
  import slow as fast` compat idiom: whichever branch runs, the effect is
  imports plus bindings, and the edge set (both branches, collected
  unconditionally as always) is pinned by condition 4.

Everything else rejects: any other conditional (even
`if __name__ == "__main__":`, whose falsity at import we could argue but
choose not to), loops, `with`, `match`, `assert` (its expression
evaluates), `raise`, `del`, augmented assignment, walrus bindings,
expression statements that are not constants, `__getattr__` or `__dir__`
or `__path__` in any form (a module `__getattr__` def makes runtime
attribute access on this module run code that a body-only diff could
change), type alias statements, and anything unparseable.

Inert expression grammar for values and defaults:

- constants, and unary/binary operator trees over constants only (operators
  on names can invoke user dunders);
- plain name loads (a global load runs no user code; module `__getattr__`
  fires on attribute access on module objects, not on loads inside the
  module itself);
- attribute access rejects, always: `a.b` can run a module `__getattr__`
  or a property;
- subscripts reject: `List[int]` calls `__class_getitem__`;
- calls reject, all of them, including `TypeVar(...)`, `namedtuple(...)`,
  `ContextVar(...)`, `re.compile(...)`, `logging.getLogger(...)`;
- f-strings reject (formatting invokes `__format__`);
- tuple/list/dict displays of inert elements are inert (construction
  allocates, nothing more); set elements and dict keys must be constants
  because they hash at construction;
- lambdas are inert when their defaults are inert; the body never runs at
  import.

Class bodies execute at import, so they get their own rules:

- no decorators (dataclass, attrs, anything: a decorator is a call). A
  measured second tier admits `@property`, `@staticmethod`, and
  `@classmethod` as bare names when the module never rebinds those names;
  they are C-level constructors with effects bounded to the descriptor.
- no keywords (a `metaclass=` keyword is arbitrary code at class creation;
  any other keyword flows into `__init_subclass__`);
- bases must be names of classes defined earlier in the same module that
  themselves passed these rules and define neither `__init_subclass__` nor
  `__set_name__`. Imported bases reject categorically; the registry
  counterexample below is why.
- body statements: docstrings, defs under the def rules above, nested
  classes under these same rules, `pass`, TYPE_CHECKING guards, and
  assignments whose targets are plain names and whose values are constants
  only. Bare names are rejected as class-body values even though they are
  inert at module level: binding an object into a class namespace invokes
  `__set_name__` on the value's type at class creation. `__slots__` with a
  literal tuple is admitted by these rules and is safe: `type()` consumes
  it without user code.
- a local class that defines `__init_subclass__` or `__set_name__` is
  itself admissible (defining a method is a binding) but is disqualified
  from serving as a base within the module.

Every degraded path is a decline, and a decline means the file simply
stays a full-impact member of the closure, which is today's behavior.

## Counterexamples attempted

Each of these was run against the prototype checker; all reject.

1. Renamed or deleted symbol in an inert module (the sharpest one). The
   init does `from .console import helper`; a diff renames `helper` to
   `run_helper` inside `console.py`. Both revisions pass any per-revision
   whitelist: the module still only binds names. But importing `pkg` at
   head raises ImportError for every consumer, including a Table-only test
   that narrowing would have skipped, and that test would have failed at
   head. No single-revision inertness checker, however strict, can reject
   this; the claim must be relational. Condition 3 (bound-name set
   equality) rejects it: the bound set changed. This counterexample is why
   inertness is defined across the pair of revisions rather than at each
   one independently.
2. Added import with side effects at a distance. A diff adds
   `from . import _plugins` to `console.py`, where `_plugins.py` is
   unchanged but has module-level side effects (it registers a codec that
   `Table` consults). `console.py` stays whitelist-inert at both revisions
   and its bound set gains only `_plugins`, but head import of `pkg` now
   executes `_plugins.py` where base did not. Condition 4 (edge-set
   equality) rejects it. Condition 3 also happens to fire here; condition
   4 is still required on its own for `import x` forms that bind the same
   name while resolving to a different file after an index change.
3. Subclass registry through a base class. `console.py` contains only
   `class ConsoleRenderer(RendererBase): ...` where `RendererBase` (in
   another module) defines `__init_subclass__` appending subclasses to a
   global registry that `Table.render` iterates. The file looks like pure
   declarations, yet renaming the class changes global state at import
   time and flips outcomes for consumers of other symbols. Rejected by the
   imported-base rule: bases must be local, hookless classes. This is the
   sharpest counterexample against the whitelist itself and the reason
   imported bases reject categorically rather than by name.
4. Descriptor `__set_name__` via class-body name assignment.
   `class C: x = tracked` where `tracked` is imported and its type defines
   `__set_name__`: code runs at class creation. Rejected: class-body
   values are constants only.
5. Enums. `class Color(Enum): RED = 1` executes EnumMeta machinery at
   import. Its effects are arguably confined to the class object, but
   proving `Enum` is stdlib `enum.Enum` requires import tracking and a
   shadow check, and member creation runs nontrivial code. Rejected by the
   imported-base rule; admitted only in the measured vetted tier below,
   with the shadow check, as future headroom.
6. `@dataclass`, attrs, `namedtuple`, `TypeVar`, `ContextVar`,
   `re.compile`, `logging.getLogger`: calls or decorators at import time.
   All rejected. Each has an individual safety argument of varying
   strength (`TypeVar` allocates an object; `logging.getLogger` mutates
   the process-global logging manager, so its argument is notably weaker),
   which is exactly why they are a vetted-allowlist discussion for later
   and not part of this design.
7. Evaluated defaults and annotations. `def f(x=compute())` runs at
   import; `def f(x: t.Thing)` without future annotations evaluates an
   attribute access. Both rejected; with `from __future__ import
   annotations` present, annotations are inert strings and anything is
   admitted there. A diff that removes the future import is caught because
   inertness is re-checked at head.
8. `if __name__ == "__main__":` blocks. The comparison is safe and the
   body is dead under pytest imports, but `python -m pkg.module` makes it
   live, and arguing import context would couple the checker to how the
   suite is invoked. Rejected as debatable.
9. Star imports inside submodules, module `__getattr__` defs, walrus at
   module level, f-strings of names, computed `__all__`: all rejected by
   the grammar above.

## Interactions

### Witnesses and closures documents

Witness schema bumps to `acquit/witnesses-v2`: the claim string gains the
narrowed variant, witnesses gain `narrowed` and `base_sha` fields, and the
closures listing is unchanged because closures do not shrink. The selection
document (v2, tree-bound per ADR 0007) is structurally unchanged; narrowed
decisions ride the same skip list. The report notes which skips were
narrowed so the PR comment can say so.

### Replay cost

Replay currently rebuilds one snapshot. With any narrowed witness present
it rebuilds two (head and base), runs the two checkers over each
intersecting file's blobs at both revisions, and computes semantic closures
in both graphs. Parsing two blobs per narrowed file is microseconds; the
dominant cost is the second snapshot, so replay lands at roughly twice
today's cost when narrowing engaged and is unchanged when it did not. The
study measured p50 analysis under a second on flask/rich/httpx, so this is
acceptable. Replay stays cache-free.

### The soundness oracle

The oracle's existing assertion (runtime-observed imports are a subset of
the static closure) remains valid unmodified, because closures never
shrink. That is also its limit: the oracle observes imports, and
`console.py` genuinely is imported, so no import-set observation can ever
distinguish a sound narrow from an unsound one. The oracle gains one cheap
structural assertion (semantic closure is a subset of the closure for
every test) and otherwise delegates narrowing validation to the mutation
arm, which observes outcomes rather than imports.

### Canary mode

Canary mode already runs everything and classifies outcomes against the
would-be-skipped set, alarming with the witness id on any failure. Narrowed
decisions therefore get live validation for free, and an unsound narrow
surfaces as a named alarm rather than a silent gap. This is the rollout
gate: narrowing ships canary-only first.

### The mutation-injection study arm

Outcome-diffing merged PRs cannot catch symbol-level unsoundness: merged
PRs had green CI, rarely flip test outcomes, and never deliberately probe
sibling-symbol isolation, so a broken narrow would sail through the replay
study unnoticed. The study gains a mutation arm that manufactures the
outcome flips the differential check needs:

- For each study repo where narrowing engages, pick import-time-only files
  under the fat init with at least one narrowed consumer.
- Mutate a function or method body in such a file (replace the body with a
  sentinel raise); separately, flip a module-level literal constant, which
  keeps the file whitelist-inert while changing a bound value.
- Run selection over the synthetic diff. Assert: every test whose home
  attribution includes the mutated file is selected; consumers of other
  symbols may be skipped.
- Run the full suite under the mutation and diff outcomes against the
  unmutated baseline. The safety check is the study's usual mechanical
  one: no outcome-changed test may live in a skipped file. Aggregation
  fails on any violation, same as the replay study.

The constant-flip case is the sharpest probe: it exercises exactly the
boundary the design claims (values changed, bindings identical), and any
hidden value-coupling through a sibling symbol shows up as a failed
skipped test.

### Rollout

Disabled by default behind configuration, promoted in stages, each stage
gated on evidence rather than time:

1. Ship the resolver seam and the checker with narrowing off. No behavior
   change, schema bumps land.
2. Enable on acquit's own repository and the study corpus in canary mode
   only: full suites still run, narrowed decisions are classified live.
3. Add the mutation arm to the study and run it across the qualifying
   repos. Zero violations required.
4. Re-run the replay study with narrowing on; zero unsafe skips required.
5. Only then allow narrowing in enforce mode, as an explicit opt-in
   documented next to `assume_inert` with the same tone: this is a proof
   with more moving parts than disjointness, and the replay gate is not
   optional for it.

## Measured rates from the census corpus

Prototype (throwaway, not shipped): the two checkers above run against the
same 61 shallow clones the census used, through acquit's own snapshot
pipeline, nothing executed. 60 repos analyzed (sympy fails snapshot,
matching the census), 55 with a fat worst init (worst top-level
`__init__.py` pinning at least half the suite), matching the census row
exactly.

Pure re-exporter rate over the 55 fat worst inits:

| Tier | Qualifying | Rate |
| --- | --- | --- |
| Strict whitelist | 15 of 55 | 27% |
| Plus star-over-literal-`__all__` | 16 of 55 | 29% |

Qualifying repos include flask, werkzeug, starlette, fastapi, typer,
hypothesis, black, uvicorn, virtualenv, tox, marshmallow, urllib3.
Top disqualification reasons: computed metadata assignments (15),
functions defined in the init (8, including click and rich), conditional
imports (7, including httpx).

Inertness rate at HEAD over the 4853 submodules of the 55 fat packages:

| Tier | Inert | Rate |
| --- | --- | --- |
| Strict whitelist | 1068 | 22.0% |
| Plus shadow-checked builtin class decorators | 1127 | 23.2% |
| Plus vetted stdlib constructors, enum/exception bases, dataclass | 1233 | 25.4% |

Median per-repo strict rate is 14%. Dominant rejection reasons: import-time
constructor calls in module-level assignments (1193), imported base classes
(1129), decorators (705). Pooled over just the 15 qualifying repos: 25.6%
strict, 28.6% vetted, with a wide spread (flask 4%, hypothesis 19%, tox
24%, virtualenv 30%, black 37%, fastapi 43%, uvicorn 44%).

## Expected win, quantified

All numbers below are estimates from committed study and census JSONs plus
the prototype rates above, under stated assumptions; none of it is a
re-replay.

The study's fat-init ceiling is the `full-graph-impact` row: run-all PRs
with no rule to blame, where the diff genuinely reaches every test. That
was 5 of 41 flask PRs, 11 of 64 rich, 20 of 83 httpx.

Checking the actual changed files of those 36 PRs against the prototype
(files taken at census HEAD, an approximation of the PR-time trees):

- rich and httpx do not qualify at all: their inits are not pure
  re-exporters (rich defines functions in the init; httpx has a
  try/except ImportError fallback def, and its star sources are behind
  it).
- flask qualifies, but of its 5 full-graph-impact PRs, one changed the
  init itself (never narrowed by design) and the rest touched files the
  whitelist rejects (flask constructs ContextVars, LocalProxies, and
  signal objects at module level in nearly every core file; 1 of 23
  package modules is inert).

Measured v1 recovery on the three study repos' actual history: zero of 36
PRs. The vetted tier flips exactly one httpx PR's files, and httpx's
impure init disqualifies it anyway.

Where the win actually lives: repos with both a pure init and a meaningful
inert share. Under the assumption that changed files distribute like the
module population (optimistic: hot files are usually the least inert,
since they define the classes), a qualifying repo recovers roughly its
inert share of core-module-only PRs, median about 20%, and each recovered
PR skips at the usual selective rates (the study's median was 93 to 97% of
suite time when selective). Corpus-wide that is roughly a quarter of
core-module changes in roughly a quarter of fat-init repos: real, but far
below what the census score of 0.917 suggested before purity and inertness
were measured. The census scored exposure; this measured recoverability,
and the gap between them is the finding.

The honest conclusion for prioritization: the claim shape, the relational
checks, and the resolver seam are worth building because they are the
foundation every narrowing needs and the mutation arm makes them testable.
The strict whitelist alone will not move the study repos. The measured
headroom is in a vetted-constructor allowlist argued item by item
(TypeVar, ContextVar, re.compile, enum bases, dataclass), which is a
follow-up gated on the mutation arm, not part of this proposal.

## The resolver framework seam

Narrowing is the first resident of `graph/resolvers/`, a seam for sound
narrowings of the rule table's over-approximations:

    class Resolver(Protocol):
        def recognize(self, site) -> Candidate | None
        def prove(self, candidate, ctx) -> Bound | Decline

`recognize` pattern-matches a hazard site (a fat init, a dynamic import
call, a sys.path mutation). `prove` either returns a bound (edges with
kinds, plus the claim fragment and replay obligations that witness the
bound) or declines, and a decline always reproduces today's fail-closed
behavior. Every resolver's claim must be re-derivable by replay from the
recorded revisions alone, which is what separates a resolver from a
heuristic.

Per the census ranking, the next residents:

- Non-literal dynamic import constant folding (rank 2, 60% of repos).
  `importlib.import_module(f"{__package__}.plugins")`,
  `__import__(BASE + name)` where the parts are module-level literal
  constants assigned once: fold the expression over single-assignment
  literals in the same module and emit `DYNAMIC_IMPORT` edges to the folded
  targets, declining on anything unfoldable (loop variables, parameters,
  config values). The proof obligation is the folding itself, re-derivable
  from the AST at replay.
- Exec-eval (rank 3, 45% of repos) is mostly not resolvable and the design
  should say so rather than promise it. `exec` of a non-literal string is
  arbitrary module acquisition; there is no bound to prove. The resolvable
  sliver is `exec`/`eval` of a string literal, which is just more source:
  parse the literal and fold its imports into edges. The census offenders
  are templating and compatibility shims over dynamic strings, so expect
  the sliver to be thin; the honest resident at rank 3 is instead sys.path
  mutation folding (rank 4, 27% of repos): prove a module-level
  `sys.path.insert` resolves to a repo-relative literal (the
  `os.path.join(os.path.dirname(__file__), "helpers")` idiom), add that
  directory as an import root, and drop the taint when every mutation in
  the file folds.

## Prototype provenance

The prototype lives outside the repository and is disposable by design:
two AST checkers (about 650 lines) plus a survey harness that reuses
acquit's snapshot pipeline, and a 36-case adversarial gallery in which
every counterexample above rejects and every whitelisted idiom passes.
Rates above are from a single run against the census clones at their
2026-08 HEADs. The shipped checker will be rebuilt inside
`graph/resolvers/` with the same tests promoted to real fixtures; nothing
from the prototype is imported.
