# Dynamic-import constant folding: analysis and evidence

Supporting analysis for [ADR 0009](../adr/0009-dynamic-import-constant-folding.md).
The ADR records the decision; this document carries the folding grammar, the
flow rules, the counterexamples attempted against them, the interaction
analysis, and the measured rates from the census corpus that bound the
expected win.

## The problem, precisely

`plugins.py` calls `importlib.import_module(f"pkg.{name}")`. The argument is
not a string literal, so the parser cannot know which module the call
acquires, R007 taints the file, and every test whose closure reaches
`plugins.py` runs on every diff. The census found the idiom in 42 of 60
analyzed repositories, test-reachable in 36 (rank 2, score 0.600), with a
median blast radius of 100% where present. Unlike the fat-init exposure, this
taint is standing: it does not depend on what changed, so it costs
selectivity on every single selection until the code changes or the analysis
learns to bound it.

Some of those arguments are not actually dynamic. When `name` ranges over a
tuple of string literals, or is a module-level constant assigned once, the
call can only ever import modules from a finite, statically visible set. The
resolver's job is to prove that set and emit one `DYNAMIC_IMPORT` edge per
member instead of the taint, exactly as if each name had been written as a
literal.

## The shape of the fix: superset edges, never a subset

The invariant that makes folding sound is directional, and it is the same
direction every acquit over-approximation points:

    fold(site) must contain every module name the site can pass to the
    import machinery in any run. Extra names are sound; a missing name
    is a lie.

An extra edge only grows closures, which only grows the selected set, and it
can only make the soundness oracle's assertion (runtime-observed imports are
a subset of the static closure) easier to satisfy. A missing name shrinks
nothing visibly: the graph looks bounded, the taint is gone, and a test that
dynamically imports the missed module can be skipped while its dependency
changes. Every rule below is therefore justified by one argument shape: the
runtime value of the folded expression is always a member of the computed
set, or the call raises before importing anything.

The "raises before importing" half does real work. A name that is unbound at
the call raises NameError and imports nothing. A key missing from a literal
dict raises KeyError and imports nothing. A folded name that fails to import
resolves through the existing literal-dynamic-import path (external, or
broken-first-party taint). In every failure case the runtime import set is
empty or a prefix chain of a folded name, and the folded edges cover it.

Folding happens at graph construction, not at selection. This is the deepest
difference from re-export narrowing and it is worth being explicit about:

- Narrowing weakens the impact rule for a closure that stays intact, so its
  claim is relational (base and head must agree) and witnesses carry new
  evidence that replay re-verifies against two snapshots.
- Folding changes which edges the graph has in the first place. The fold is
  a pure function of one module's source bytes plus that module's identity
  under the import roots. Each revision's graph carries its own folds, the
  graph hash covers the result, and replay already rebuilds every snapshot
  from scratch, so replay re-derives every fold with zero new machinery.
  Witness schema, claim strings, and the selection document are untouched.

## The folding grammar

A site is any occurrence the parser already flags as
`non-literal-dynamic-import`: a non-literal `importlib.import_module` or
`__import__` call, a non-literal `sys.modules` subscript, or a non-literal
`sys.modules.get/setdefault/pop` call. The folder tries to evaluate the name
argument to a finite set of strings. Every rule is a whitelist; anything
outside it declines the whole site, and a decline reproduces today's taint
exactly.

Closed string expressions, evaluated to value sets:

- a string literal: the singleton set;
- concatenation (`+`) of closed expressions: the cross product, capped (a
  fold that exceeds 128 names declines as an explosion);
- an f-string whose replacement fields are closed expressions with no
  conversion and no format spec (`!r` or `:>10` reject: both call into
  formatting protocols whose output the folder would have to simulate);
- a conditional expression or boolean operation over closed expressions:
  the union of both arms. `"tomli" if sys.version_info < (3, 11) else
  "tomllib"` folds to both names; the runtime value is always one of them,
  and the untaken arm is an extra edge, which is sound;
- a name, resolved by real scope rules (below);
- `__package__` and `__name__`, as anchors (below).

The constant environment, with its flow rules spelled out:

- A name reference is first resolved to its owning scope the way Python
  resolves it: the innermost function scope that binds it, else enclosing
  function scopes, else the module scope, skipping class scopes for
  references not directly in the class body, honoring `global` declarations.
  Text matching is not scope resolution; the shadowing counterexamples below
  are why the folder must implement the real rules.
- A name folds when every binding occurrence in its owning scope is a plain
  assignment of a string literal. Binding occurrences are all of them:
  assignment targets, augmented assignments, `del`, walrus targets, `for`
  and `with ... as` and `except ... as` targets, import aliases, `def` and
  `class` statements, match captures, function parameters, and, for the
  module scope, any assignment governed by a `global` declaration inside
  any function. One non-literal binding anywhere poisons the name.
- The folded value is the union of all the literal bindings. Single
  assignment is the common case, but it is not the rule: the rule is that
  every binding is a literal. A name bound `"pkg.fast"` at module level and
  `"pkg.slow"` inside an `if` folds to both. At any program point the name
  is either unbound (NameError, no import) or holds one of the literals, so
  the union is a superset regardless of control flow, assignment order, or
  which revision of the value a closure observes. This subsumes the
  single-assignment rule and costs nothing, because enumerating every
  binding site is already required for the poisoning check.
- Nonlocal writes poison the name in every enclosing function scope.
  Cross-module constants (`from .constants import BASE`) decline in v1: the
  fold would depend on a second file's contents, which breaks the
  one-blob-one-fold cache story and doubles the replay derivation surface.
  The seam for v2 is real but it is a different, relational obligation; the
  corpus measured zero occurrences, so it stays a seam.

Enumerable loop variables:

- A name whose only binding in its owning scope is a single `for` target (or
  comprehension target) over an enumerable iterable folds to the iterable's
  element set. Because that is the name's only binding, any read, including
  from a nested closure or after the loop ends, sees either unbound or an
  element. `break`, `continue`, an empty iterable, or a partially consumed
  generator only shrink the runtime set, and generators' laziness means at
  most the folded imports happen, never others. Extra edges again.
- Enumerable iterables: an inline tuple, list, or set display of closed
  strings (the display is a fresh object no other name can alias); an inline
  literal dict display (iteration yields its keys, which must then be closed
  strings; the values may be arbitrary expressions because they never flow
  into the name); a name whose every binding is a tuple display of string
  literals. Tuples only, by name: a tuple of strings is immutable all the
  way down, so aliasing is harmless. A named list or set display is a
  mutable accumulator and rejects no matter how literal it looks; the
  sharpest counterexample below is exactly this shape.
- Tuple-unpacking loop targets fold per position when the iterable is an
  inline display of same-length literal tuples.

Literal dict registries (specified, prototype-validated, zero corpus hits at
the census HEADs; retained because the derived-registry follow-up below
builds directly on this escape analysis):

- A module-level name bound exactly once to a dict display qualifies as a
  registry only if every other mention of the name in the module is a
  whitelisted read: a subscript load, a `get`/`keys`/`values`/`items`/`copy`
  call, a membership or comparison test, a loop iterable, or an argument to
  an unshadowed `len`/`sorted`/`list`/`tuple`/`set`/`frozenset`/`iter`/
  `reversed`. Any other mention (a bare argument, another assignment, a
  subscript store, `update`, `setdefault`, `pop`, `del`) may alias or grow
  the dict and rejects it. Removal-only mutations would keep the value set a
  subset, but proving "removal-only" is not worth the argument; declines are
  cheap.
- Subscripting a registry (or an inline literal dict) folds to the union of
  its values, and the key expression does not need to fold at all: a plain
  dict either returns one of the display's values or raises KeyError and
  imports nothing. Key-side enumerability is irrelevant to value-side
  folding, and vice versa: iterating a registry folds to its keys even when
  the values are arbitrary. `get` adds the default's fold to the value
  union; `get` with no default returns None, and `import_module(None)`
  raises before importing.
- This is also the honest answer to platform-conditional imports.
  `f"pkg.{sys.platform}"` declines: `sys.platform` is an environment string
  with no static bound. But the idiomatic literal mapping,
  `{"linux": "epoll", "win32": "select"}[sys.platform]`, folds to the value
  union, because enumerability lives in the values, not in the key. The
  folded set covers every platform's arm; arms for other platforms are
  extra edges.

Anchored dunders:

- `__package__` and `__name__` in leading position fold as symbolic anchors
  that graph construction resolves against the module's identities under
  the import roots, exactly the way relative static imports already resolve
  through `_package_parts`, with the same fail-closed union across multiple
  identities and a decline when the file is under no root. This keeps the
  fold a pure function of (blob, identity) and reuses machinery that is
  already replay-covered. The `python -m` caveat (where `__name__` is
  `"__main__"`) is outside the import-graph model in precisely the way it
  already is for static imports: tests acquire modules through the import
  machinery, where the identity holds; a suite that runs modules as scripts
  does so through subprocess or runpy, which the graph has never claimed to
  model. Dunders in non-leading positions decline.

Call-shape rules:

- `import_module(name, package)`: if any folded name is relative, the
  package argument must itself fold (a literal, a constant name, or
  `__package__`), and each relative name resolves against each package value
  with the same algorithm literal relative targets use today. Any
  unresolvable pair declines the site.
- `__import__(...)`: `level` must be absent or the literal 0. `fromlist`
  must be absent, an empty display, or a display of closed strings; each
  fromlist entry adds `name.entry` to the fold, because `__import__` with a
  fromlist imports submodules named there. `globals` and `locals` arguments
  are ignored: with level 0 they do not affect which module is imported.
  Anything else about the call declines.
- `sys.modules[key]` and `sys.modules.get/setdefault/pop(key, ...)`: the key
  folds under the same grammar; a relative-looking folded key declines.
  Reading `sys.modules` acquires a module exactly like a dynamic import
  does, which is how the parser already treats the literal case.

Callee provenance, required before any of the above applies:

- The detector matches callees by name on any receiver, which is sound for
  tainting (a look-alike costs precision, a miss costs soundness) and fatal
  for folding: replacing the taint on `loader.import_module(name)` with
  edges asserts something about a method the folder knows nothing about.
- `import_module` folds only when the name resolves to bindings that are all
  `from importlib import import_module` (aliases included), or when the
  receiver is a name whose bindings are all `import importlib` (submodule
  imports of `importlib.x` bind the same name and also qualify).
- `__import__` folds only when the name is unbound everywhere in the module,
  which means the builtin. One shadowing binding declines every site.
- `sys.modules` folds only when the receiver is exactly `sys.modules` and
  `sys` resolves to bindings that are all `import sys`. The detector's
  `endswith(".sys.modules")` look-alikes stay tainted.

Out of the v1 grammar, with measured demand: percent formatting (one corpus
occurrence, itself unfoldable for other reasons), `str.format` (zero),
`str.join` (three, all over dynamic sequences), iterator wrappers like
`sorted(...)` in the loop header (zero folds needed them), walrus iterables
(zero), and string methods on literals (two: a `.replace` and an `.lstrip`).
Each of these is a small, self-contained extension with its own soundness
argument; none of them earned its checker surface in this corpus. String
methods deserve their explicit rejection rationale: `("pkg." + k).upper()`
is computable in principle, but evaluating string methods at fold time makes
the fold a function of the analyzing interpreter's Unicode tables rather
than of the source bytes (case mappings change across Unicode versions, and
analysis and test interpreters can differ), which breaks the rule that a
fold is re-derivable byte-for-byte by any replay. Codepoint concatenation
has no such dependency; method evaluation rejects categorically.

## Partial folds decline entirely

Where enumerability fails partially, the site declines as a whole. The
literal arm of `for n in ("a", "b", discover()):` folds to nothing, because
the dynamic residue can import anything, so no finite edge set bounds the
site; emitting edges for the literal arm while dropping the taint would be
exactly the subset lie the invariant forbids.

The remaining option, emitting the literal arm's edges while keeping the
taint, is sound but worthless, and it is worth recording why. The taint
already pins every test whose closure reaches the file. An extra edge out of
a tainted file can only extend the closures of tests that already reach the
file, and every one of those tests is already pinned; a test that does not
reach the file cannot acquire reachability through an edge that starts
there. Selections are therefore identical with or without the partial edges,
while the graph grows edges that look like a bound and are not one. The same
argument covers files with several sites: each site folds completely or
declines completely, folded sites contribute edges, and the file sheds its
taint only when every dynamic site in it folds and no other suspect kind
remains. Attribution is the one place partial edges could ever matter (a
report could name the folded arms), and that is not worth a graph full of
half-bounds.

## Unresolvable folded names

A fold produces names, not files. Each name feeds the exact pipeline literal
dynamic imports use today: resolve against the index; first-party names
become `DYNAMIC_IMPORT` edges plus the usual prefix edges (an import of
`a.b.c` that fails at `c` has still executed `a` and `a.b`, and the prefix
edges cover that); names whose top level is not first-party become external
edges; names that look first-party but do not resolve keep the importer
tainted, surfacing through R011 when the file is changed. No new rule and no
new semantics: a folded name is a literal name that happened to need
proving. The measured mix over every folded name in the corpus: 23
first-party, 22 external, zero broken.

## Counterexamples attempted

Each of these was run against the prototype folder; all decline.

1. The accumulator that starts literal (the sharpest, and it is not
   hypothetical). django's template-library discovery does `candidates =
   ["django.templatetags"]`, then `candidates.extend(f"{app_config.name}
   .templatetags" for app_config in apps.get_app_configs())`, then
   `import_module(candidate)` in a loop over it. An early prototype admitted
   list displays whose elements are all literals into the constant
   environment and folded the loop to exactly `{"django.templatetags"}`: a
   strict subset of what runtime imports, wearing a proof's clothes. The
   same shape appears as the empty accumulator, `to_restore = []` appended
   inside a `try` and drained through `sys.modules[name] = mod` in the
   `finally` (sqlalchemy's test utilities), where the vacuous "all elements
   are literals" check folds the loop to the empty set and deletes a real
   taint outright. The rule that survives: only immutable displays qualify
   by name (tuples of strings), and mutable displays fold only inline at
   the use site, where no name ever aliases them. This counterexample is
   why the environment admits tuple displays and nothing else.
2. The constant that is not. `MODE = "pkg.default"` at module level, plus
   `def set_mode(v): global MODE; MODE = v`. The top of the file shows a
   perfect literal constant; the rebinding lives in a function body under a
   `global` declaration. Every binding-collection shortcut (scanning only
   module-level `ast.Assign` statements) folds this. The walrus variant,
   `if (NAME := compute()):` after a literal `NAME = "pkg.a"`, defeats the
   same shortcut. The flow rules enumerate every binding construct for
   exactly this reason; the check is only as sound as its binding census.
3. Local shadowing. A module constant `NAME = "pkg.safe"` and a function
   whose parameter or local assignment is also called `NAME`, with the call
   site inside the function. Folding the module constant is wrong; the local
   value is arbitrary. Scope resolution, not name matching.
4. Registry aliasing and mutation. `_REG = {"a": "pkg.a"}` then
   `register_plugins(_REG)` or `_REG.update(entries)` or `_REG["b"] =
   discover()`. The display is fully literal; the object is not. The mention
   whitelist rejects the name at the first non-read mention.
5. Look-alike callees. `self.import_module(spec)` on a plugin manager, and
   `fake.sys.modules[key]` behind an attribute chain. Detection is
   deliberately name-based and over-approximate; folding without provenance
   would convert someone's method into a false bound. Provenance rules
   reject both, and the shadowed-`__import__` module (a tracing shim bound
   at module level) rejects with them.
6. Fold-time string evaluation. `("pkg." + name).upper()` with `name`
   fully literal: rejected not because the value is unknowable but because
   the knowledge lives in the wrong interpreter, per the Unicode argument
   above. Same rejection for `.lower`, `.strip`, `.replace`, all of them.
7. `sys.platform` in an f-string. Declines as an unbounded name; the
   literal-mapping form folds by value union as argued in the grammar. The
   distinction is measured, not stylistic: the direct f-string form appears
   in the corpus, the mapping form does not.
8. Rebound loop variables. `for n in ("a", "b"): n = alias(n);
   import_module("pkg." + n)`. The loop target is no longer the name's only
   binding, so it declines; the transformed value is arbitrary.
9. Relative residue. A folded name starting with a dot whose package anchor
   does not fold, and `__import__` with a nonzero level (whose resolution
   depends on runtime globals). Both decline; relative resolution happens
   only through the same anchored machinery static relative imports use.

## Interactions

### Placement and caching

Folding runs inside fact extraction, where the AST is already in hand: the
parser flags the site, the folder tries to prove it, and `ModuleFacts` gains
a `folded_dynamic_imports` field (site line, pattern, names, anchored
forms) while the suspect list keeps only the sites that declined. Folded
names with anchors resolve at graph construction against the index, exactly
where relative static imports resolve today. The parse cache serializes the
new field, so `CACHE_FORMAT_VERSION` bumps and R017 silently invalidates old
entries. Edge sets change wherever a fold fires, so `GRAPH_SCHEMA_VERSION`
bumps too (it coordinates with the narrowing bump: whichever lands second
takes the next number; the graph hash makes the ordering unambiguous).

The A1 assumption in soundness.md currently reads "static import statements
or literal dynamic imports"; it gains "or dynamic imports whose argument
provably folds to a finite literal set", and R007's rules.md entry notes
that foldable sites resolve into edges instead of tainting. Both edits ride
the implementation change, not this proposal.

### Witnesses, replay, and cost

None of the witness machinery changes. The claim strings, the selection
document, and the closures listing are untouched, because folding is fully
absorbed into the graph that all of them already hash and rebuild. Replay
cost is unchanged: the fold re-derives during the snapshot rebuild replay
already performs, from the blob bytes and the roots, with no second
snapshot and no recorded evidence beyond the graph itself. This is the
cheapest possible resolver shape, and it is worth protecting: any future
folding tier that needs cross-file inputs (the v2 seams below) loses this
property and must justify its own evidence plumbing.

### The soundness oracle

Narrowing had to lean on the mutation arm because no import observation can
distinguish a sound narrow from an unsound one. Folding is the opposite: the
claim is precisely about which imports a site performs, and the oracle
already observes runtime imports. Today a tainted fixture file is the
documented exception where the oracle only checks that reaching tests are
forced to run; once its sites fold, the file stops being an exception and
the oracle's main assertion (observed imports are a subset of the closure)
directly validates the fold on every oracle run. The fixture suite gains
foldable-idiom repos (loop over a literal tuple, constant name, literal
registry, `__package__` anchor, and a deliberate near-miss that must stay
tainted), so an unsound fold in the checker fails CI at the first oracle
run, not in a study.

### Canary mode

Unchanged and inherited for free: folded graphs produce selections, canary
runs everything and classifies outcomes against the would-be-skipped set,
and an unsound fold that matters surfaces as a named alarm. Folding ships
canary-only first, like every narrowing.

### The mutation-injection study arm

Outcome-diffing merged PRs has near-zero power here for the same reason as
narrowing, so the study arm manufactures its probes. For each study repo
where folding fires on a test-reachable file F with folded target set T:

- Positive probe: mutate each first-party member of T (sentinel raise in a
  function body; separately, flip a module-level literal constant). Assert
  every test whose closure includes F is selected, and run the suite under
  the mutation asserting the mechanical rule: no outcome-changed test may
  live in a skipped file. This validates that the folded edges exist and
  carry impact end to end.
- Adversarial probe: mutate sibling modules of T's members (same package,
  not in T), the plausible misses a buggy folder would drop. Selection under
  a diff touching the sibling may now skip tests that reach F; the full-run
  outcome diff catches any test that actually imported the sibling through
  the folded site. A violation here is a folder bug by construction.
- Import-log probe (folding-specific, cheapest): run the suite under the
  oracle's recorder and assert every runtime import performed from F lands
  inside F's static edges plus folded edges. This tests the fold claim
  directly rather than through outcomes.

The honest limit: probes validate the arms that execute on the study
machine. A platform mapping folded to a union includes arms no CI runner
takes; their coverage rests on the value-union argument, not on any
observation, and the design accepts that explicitly.

### Rollout

Same bar as narrowing, gated on evidence:

1. Ship the folder inside `graph/resolvers/` with folding off. Schema bumps
   land; no behavior change.
2. Enable on acquit's own repository and the study corpus in canary mode;
   oracle fixtures with foldable idioms land in the same change.
3. Mutation arm across the repos where folding fires. Zero violations
   required, positive and adversarial probes both.
4. Replay-study re-run with folding on; zero unsafe skips required.
5. Enforce mode, opt-in, documented next to `assume_inert` with the same
   tone: this proof is cheaper than narrowing's, but the gate order is not
   negotiable.

## Measured rates from the census corpus

Prototype (throwaway, not shipped): the recognizer and folder above, run
over the same census clones through acquit's own snapshot pipeline, nothing
executed. 60 repos analyzed (sympy fails snapshot, matching the census).
Every one of the 579 non-literal dynamic-import occurrences the census
counted was classified: folded with names, or declined with a reason.

Occurrences and foldability:

| Population | Sites | Foldable | Rate |
| --- | --- | --- | --- |
| All occurrences (42 repos) | 579 | 27 | 4.7% |
| Test-reachable occurrences (36 repos) | 516 | 23 | 4.5% |

By call shape: 355 `import_module`, 136 `sys.modules[...]`, 58 `__import__`,
30 `sys.modules` method calls. Per-repo foldable share: median 0%, mean
7.4%; 31 of the 42 repos fold nothing, one (nox) folds everything it has.

Foldable patterns by frequency, with real examples:

| Pattern | Sites | Examples |
| --- | --- | --- |
| `sys.modules[__name__]` self-reference | 11 | `kombu/__init__.py:93`, `django/apps/registry.py:24`, `nox/virtualenv.py:389`, `pygments/formatters/__init__.py:153` |
| module or local literal constant | 10 | fastapi `tests/test_tutorial/test_debugging/test_tutorial001.py:15` (`MOD_NAME`), django `tests/messages_tests/tests.py:78`, pytest `doc/en/example/assertion/failure_demo.py:203` |
| loop over a literal display | 5 | requests `src/requests/compat.py:42` (`for lib in ("chardet", "charset_normalizer")`), requests `src/requests/packages.py:9` (`for package in ("urllib3", "idna"): __import__(package)`), pipx `tests/test_run.py:272` |
| conditional expression | 1 | pipx `src/pipx/commands/manifest.py:40` (`import_module("tomli" if sys.version_info < (3, 11) else "tomllib")`) |

Registry subscripts over inline or named literal dicts: zero corpus hits.
The registries that exist in the wild (kombu, celery, historical werkzeug)
are loop-built, covered under headroom below.

Decline reasons, dominated by names that are dynamic on principle:

| Reason | Sites | Character |
| --- | --- | --- |
| parameter or non-literal local/global | 243 | `import_module(mod_str)` where the name arrives as an argument |
| attribute access | 198 | `import_module(settings.SESSION_ENGINE)` and friends: configuration |
| rebound names | 57 | literal somewhere, rebound elsewhere |
| dynamic iterables | 34 | loops over `sys.modules`, parameters, computed lists |
| loop-built registries | 9 | kombu and celery lazy-init dicts |
| join or percent over dynamic parts | 4 | dotted-path splitters |
| provenance failures | 2 | look-alike receivers, one genuine `sys` ambiguity |
| other (string methods, calls in the key) | 5 | |

That is the finding: 76% of all occurrences (441 of 579) take their name
from a parameter or an attribute, which no sound static fold can bound. The
foldable core is small on principle, not because the grammar is timid; a
maximally clever folder tops out near 10% of sites in this corpus.

Blast-radius weighting, the number that matters for selection:

| Measure | Before | After folding |
| --- | --- | --- |
| carrier files (test-reachable) | 369 | 361 still tainted by this kind |
| files fully de-suspected | | 8 reached (11 total, 9 lose taint outright) |
| median R007 blast radius over the 41 repos with tests | 100% | 100% |
| repos where the R007 pin drops at all | | 3 of 42 |
| repos where it drops to zero | | 2 (pipx, nox) |
| median overall taint blast radius | 100% | 100% |

pipx and nox shed their R007 pin entirely (both had two or three sites, all
foldable or unreached); fastapi drops from 30.7% to 30.5% (one tutorial test
file de-taints). Everywhere else the folded sites sit in files that carry
other unfoldable sites or other suspect kinds, so the taint survives the
fold. Overall taint medians do not move at all: exec-eval and sys.path
suspects keep the same files pinned in most repos.

## Expected win, quantified

Against the taint status quo the honest statement is: constant folding as
specified proves 27 of 579 sites, folding them to 45 names' worth of sound
edges, permanently removes the R007 pin in two corpus repos out of 42,
trims one more, and changes nothing else. The census rank-2 score of 0.600 measured exposure
(how often the idiom pins tests); this measured recoverability, and the gap
is even wider than it was for narrowing: exposure said 60% of repos, the
sound fold reaches 4.7% of sites and moves selection in 5% of affected
repos. Non-literal dynamic imports are overwhelmingly configuration-driven
plugin loading, and their names genuinely are not in the file.

The identified headroom, in measured order:

- Derived registries: dicts built by a module-level loop over a literal
  structure, `object_origins[item] = _module` over `all_by_module.items()`.
  This is the actual lazy-init idiom in the wild (kombu's and celery's
  package inits, both 100%-blast repos, 9 sites). Folding it means proving
  a dict comprehension or accumulation loop populates only literal-derived
  values, a strictly harder escape argument than the display rule, on top
  of exactly the registry machinery specified here.
- Cross-module constants: zero corpus occurrences; not worth its relational
  cost today.
- The string-method and format tiers: seven occurrences combined, most
  unfoldable for other reasons too.

None of these change the shape of the conclusion: the pattern is that the
dominant decline mass is dynamic on principle.

## The resolver framework seam

Folding is the second resident of `graph/resolvers/` and the first
graph-construction-time one. It fits the protocol narrowing established:
`recognize` matches a hazard site (here, the parser's own suspect record),
`prove` returns a bound (the folded name set, with the pattern recorded for
the report) or declines into today's fail-closed behavior. What it shares
with narrowing is the part worth standardizing: declines are always sound,
bounds carry their derivation, and the derivation is re-derivable by replay
from recorded revisions alone. What it deliberately does not share: no
relational conditions, no witness fields, no second snapshot. The seam
should make that an explicit axis (a resolver declares whether its proof is
per-revision or relational) so replay knows what each resident owes it, and
so the next resident (sys.path-mutation folding, which is per-revision but
index-dependent) lands on the right side of the line by construction.

## Prototype provenance

The prototype lives outside the repository and is disposable by design: a
scope-aware folder (about 950 lines) plus a survey harness reusing acquit's
snapshot pipeline, and a 37-case adversarial gallery in which every
counterexample above declines and every whitelisted idiom folds to the
expected names. Rates are from a single run against the census clones at
their 2026-08 HEADs; the classification of all 579 sites reconciles exactly
against the census suspect counts. Two of the gallery's decline cases were
found the hard way, as unsound folds the first prototype run produced on
django and sqlalchemy, which is the strongest argument this document has
for the mutable-display rule. The shipped folder will be rebuilt inside
`graph/resolvers/` with the gallery promoted to real fixtures; nothing from
the prototype is imported.
