# Derived registries: analysis and evidence

Supporting analysis for [ADR 0010](../adr/0010-derived-registries.md).
The ADR records the decision; this document carries the extended grammar,
the soundness arguments, the counterexamples attempted against them, the
corrections the hunt forced on ADR 0009's rules along the way, and the
measured rates from the census corpus that bound the expected win.

## The problem, precisely

ADR 0009 declined nine subscript-shaped sites as "loop-built registries,
kombu and celery lazy-init dicts" and named them the measured headroom worth
designing next. The idiom, in kombu's package init verbatim:

    all_by_module = {
        'kombu.connection': ['Connection', 'BrokerConnection'],
        'kombu.entity': ['Exchange', 'Queue', 'binding'],
        ...
    }
    object_origins = {}
    for _module, items in all_by_module.items():
        for item in items:
            object_origins[item] = _module

    class module(ModuleType):
        def __getattr__(self, name):
            if name in object_origins:
                module = __import__(object_origins[name], None, None, [name])

The registry is built by a module-level loop instead of written as a
display, so ADR 0009's literal-registry rule (single dict-display binding,
every other mention a whitelisted read) rejects it at the first subscript
store. But the values that ever reach `__import__` are exactly
`all_by_module`'s keys: eight string literals sitting in plain sight. The
resolver this document specifies proves that, and measures how often the
proof applies.

One correction before anything else: the "9 sites" in ADR 0009's decline
table was the population of the prototype's `subscript` decline bucket, not
a count of the idiom. Re-examined site by site, the bucket decomposes into
3 lazy-init registry consumers (kombu's init, celery/local.py twice), 3
pytest fixture stores over computed lists (test_pytester.py), 1
walk_packages discovery loop (django's template backends), 1 sphinx hook
parameter (pipx docs/conf.py), and 1 filesystem-driven example (werkzeug's
coolmagic). Only the first three are registries at all, and of those three
only kombu's is provable, for reasons argued below. The headroom this ADR
was asked to measure was mislabeled at birth, and the honest recount is
part of the answer.

## The shape of the fix: union over every store

The invariant is ADR 0009's, unchanged: the fold must contain every module
name the site can pass to the import machinery in any run, or the site
declines. What changes is how a dict earns a bounded value set.

A module-level name is a derived registry when:

- its module-scope binding census contains exactly one binding, and that
  binding is a dict display (empty or populated) or a dict comprehension
  whose value expression folds;
- every other mention of the name, anywhere in the module, is either a
  whitelisted read (ADR 0009's list: subscript load, get/keys/values/items/
  copy, membership tests, loop iterables, unshadowed len/sorted/list/tuple/
  set/frozenset/iter/reversed arguments) or a recognized derivation store:
  a plain subscript store `R[key] = value` whose value expression folds, or
  `R.update(...)` of a dict display or comprehension whose values fold;
- assignment of the registry to another module-level name is admitted only
  when the target has a single binding and itself passes this whole scan;
  the two names then form one family denoting one dict, and stores through
  either count. Anything else that could alias the object (a bare argument,
  an attribute store, a tuple target, del, an augmented store) rejects.

The registry's value set is the union of the display's values, every
store's folded value set, and every update argument's folded value set. A
subscript load or `get` over the registry folds to that union; the key
expression is still irrelevant, by the same argument as ADR 0009 (a plain
dict returns a stored value or raises KeyError before any import happens,
and a dict display or comprehension always builds the exact builtin dict,
so no subclass can intercept the subscript).

Two properties carry the soundness argument:

- The union quantifies over store statements, not executions. Whether a
  store runs zero times or a thousand, under an `if` or in a function
  nobody calls, before the consumer or after it, the dict can only ever
  hold values some store (or the display) could produce, and every one of
  those values is in the union. Control flow, iteration order, and timing
  are all irrelevant; conditional inserts and dead registration helpers
  cost nothing. This is the same argument shape that lets ADR 0009 fold a
  name from all its literal bindings regardless of which one is live.
- Key transformations are free because keys never feed the importer. The
  loop may build keys by any transformation it likes (`R[k.upper()] = v`,
  `R[alias_map.get(k, k)] = v`); the value side alone needs the proof. The
  symmetric trap is worth naming: the same transformation on the value side
  (`R[k] = ("pkg." + k).upper()`) still declines, because evaluating string
  methods at fold time couples the fold to the analyzing interpreter's
  Unicode tables, ADR 0009's standing rejection.

Store values themselves fold under the v1 grammar plus three extensions
the idiom needs:

- loop variables over a literal dict's `.items()` fold per unpack position,
  lazily: position zero is the key set, position one the value set, and a
  keys-only consumer no longer pays for unfoldable values (kombu's values
  are list displays, which fold as nothing but iterate fine);
- a loop variable that iterates another loop variable (`for _module, items
  in D.items(): for item in items`) folds to the element union of the
  outer dict's value displays, when every value is an inline display of
  closed strings;
- a name whose bindings are all plain assignments of foldable expressions
  folds to the union of those folds (transitive local constants), which
  catches the `mod = REG[key]; import_module(mod)` spelling. In value
  position this is safe as-is, because only strings ever fold there; in
  iteration position it must refuse mutable displays, for a reason the
  corpus demonstrated on the first run (counterexample 2 below).

## The fromlist coupling

kombu's consumer is `__import__(object_origins[name], None, None, [name])`:
the fromlist is not literal, so ADR 0009's call-shape rule declines the
whole site even with the registry proven. The coupling rule recovers it:

- Arguments evaluate left to right. If `R[name]` did not raise KeyError,
  then `name` held a key of `R` when the fromlist display was evaluated,
  and a plain local name cannot be rebound between the two reads except by
  a walrus inside the call expression, which rejects (and independently
  poisons the name's binding census).
- So when the first argument is a subscript of a registry with a fully
  enumerable key set, and a fromlist element is the same plain name as the
  subscript key, that element is bounded by the key set.

The entries pair with the folded module names as `(module, entry)` and
resolve with from-import semantics: an edge only when `module.entry` is an
indexed module, nothing otherwise, exactly how `from module import entry`
resolves today. Resolving them as absolute dotted names would read every
attribute-shaped entry as broken first-party and keep the file tainted,
which silently wastes the fold. ADR 0009 specified fromlist entries as
dotted `name.entry` strings; that spelling was harmless there because no
folded corpus site carried a fromlist, but the derived tier trips over it
immediately, so the from-import resolution is part of this design. In
kombu every one of the 120 pairs is an attribute of its module (classes
and functions, not submodules), so the pairs add no edges beyond the value
modules' own and no broken names; they exist to keep the superset honest,
because a fromlist entry that happens to name a real submodule does import
it.

The pairing is over-approximated as values times keys rather than tracked
per store (relational key-to-value correlation is real analysis for zero
measured benefit), and the whole fold stays under the 128-name cap; kombu
lands on 128 exactly (8 names plus 120 pairs), which says the cap needs
headroom or a separate pair budget before this grammar ships anywhere.

## What the hunt broke along the way: corrections to ADR 0009

The adversarial pass on the derived grammar found three holes that are not
about derived registries at all. They apply to ADR 0009's v1 rules, and
one of them is exercised by the flagship repo itself.

**Unknown-key globals stores (the STATICA_HACK hole).** kombu's init
contains, verbatim:

    STATICA_HACK = True
    globals()['kcah_acitats'[::-1].upper()] = False

A store through `globals()` (or `vars()`, module-level `locals()`, a
module's `__dict__`, direct or setattr attribute stores through
`sys.modules` entries) rebinds a module-scope name with no binding
occurrence the scope census can see. ADR 0009's name rule enumerates
binding constructs; this is none of them, and it defeats the census
exactly the way the `global`-declaration counterexample did, one layer
further out. The sound repair is a poison ladder over such stores:

- a literal string key poisons exactly that name;
- a key the v1 grammar itself bounds poisons exactly those names (with one
  guard: a literal-key store that rebinds a name the key fold depended on
  makes the fold stale and escalates to full);
- an unknown key whose stored value is a non-string constant poisons only
  string-coercion contexts (f-string fields, str.format arguments, percent-s
  slots), because a value like False can only become a module-name string
  through str(); registry subscripts, direct name arguments, and plain
  concatenation all raise on it before any import (`"pkg." + False` is a
  TypeError, `False[key]` is a TypeError, `import_module(False)` raises),
  and a raise imports nothing;
- an unknown key with any other value poisons every module-scope read in
  the module, including the anchors and the callee provenance itself,
  because `globals()['__import__'] = shim` is in scope of what such a
  store can do;
- one exemption: `update(X.__dict__)` where X provably denotes the running
  module (a `sys.modules[__name__]` read, or a name bound once to one)
  writes, for every name, the module's own current value of that same
  name, because dict-to-dict update preserves the key-value pairing; every
  such value is already in the binding census, so the store poisons
  nothing whatever its receiver is.

Each refinement earned its place against a real file, and this is the part
worth reporting: the flat rule ("any such store poisons the module") is
sound and kills seven published v1 folds. kombu's False store would kill
its own two self-reference folds without the value-type rung. requests'
packages.py runs `locals()[package] = __import__(package)` in a loop over
two literals; the key-fold rung poisons exactly urllib3 and idna and the
published fold of the loop's own import survives. pygments replaces its
formatters and lexers modules with `newmod.__dict__.update(oldmod.__dict__)`
where oldmod is `sys.modules[__name__]`; the identity-preserving-update
exemption keeps its four published self-reference folds alive. With the
full ladder the corpus loses zero published v1 folds (measured: all 27
survive the corrections), and the repair is what it should be: a closed
hole with no rate change. It should land in the production folder before
folding ships, because the hole is
one `globals()[key] = "module.name"` away from a subset lie and the
census cannot see the key. What stays outside the ladder, documented as
accepted residue with the oracle as backstop: attribute stores through
aliases of the running module (`m = sys.modules[__name__]; m.MODE = x`
one assignment apart), and everything getattr- or exec-shaped.

**The unpack-position conflation.** The 0009 measurement prototype folded
`for k, v in ("ab", "cd")` by handing position zero the element union
{"ab", "cd"}, while the runtime unpacks the strings character by character
and k is "a". Tuple targets must fold only over iterables whose element
shape provably matches the target arity (displays of same-length literal
tuples, dict items() for two-name targets); everything else declines as
unpack-shape. No published number depended on the bug (no folded corpus
site used a tuple target), and the production folder already guards this
correctly; it is recorded here because the gallery should have caught it
and now does.

**Module attributes are writable from anywhere (the observer lesson,
again).** Every fold that reads module-scope state (a constant name, a
named tuple, a registry, an anchor) implicitly assumes no other module
rebinds that state through an attribute store: `import kombu;
kombu.object_origins['x'] = 'evil.mod'` is three tokens of ordinary Python
and no per-module binding census can see it. ADR 0008's revision taught
exactly this lesson: binding structure of one file is not the whole trusted
surface; behavior of the files that can observe (here: mutate) it is part
of the claim, and it must be checked, not assumed. The check is a
construction-time join: each module's facts record, generically and per
blob, its mutating mentions of imported-module attributes (attribute
stores, subscript stores through attributes, mutating method calls,
bare-object escapes, from-imports of the name, getattr/setattr with a
matching or unknown attribute, stores through non-literal sys.modules
subscripts); graph construction intersects those mentions with the names
each fold depends on, which the folder reports per site.

What a hit means is a semantics decision, and the corpus forced it
immediately: kombu's own test conftest contains

    setattr(sys.modules[parent], attr, module)

inside a module-mocking fixture, where parent and attr are computed. That
is statically capable of rebinding kombu.object_origins, so under a
"decline the fold" reading, the flagship fold dies at the hands of its own
test suite, and the measured win of the entire tier is zero everywhere.
The reading this design specifies instead follows the precedent the rule
table already sets for sys.modules stores: the mutation taints the
mutating file. The soundness argument is the closure: a mutation can only
execute in a test's process if the mutating file is in that test's import
closure, so tainting the mutator pins every test that could observe the
mutation; a test whose closure misses the mutator can only be affected
through cross-test session leakage, which assumption A4 already excludes
(and rules.md already accepts attribute monkeypatching from tests on
exactly this argument; registries are the one case where an attribute
store changes module acquisition, which is why the mutator needs the taint
that ordinary monkeypatching does not). Channels no scan can see (getattr
chains built from strings, exec, module objects laundered through
containers and call returns) remain the same accepted residue every
detector carries, with the same backstop: the oracle observes runtime
imports directly, and a registry fold is precisely an importable claim.

Both semantics were measured. Strict declining: 21 of the 28 derived-mode
folds survive corpus-wide, and the seven that die include every fold this
tier added (kombu's three init folds at its conftest's hands, pygments'
four self-reference folds at test_basic_api's), so strict semantics zero
the tier exactly. Mutator tainting: all 28 folds stand and two files
corpus-wide acquire the mutator taint, both test files (kombu's
t/unit/conftest.py, already tainted by its own six unboundable sites, and
pygments' tests/test_basic_api.py, which escapes the formatters and lexers
modules through getattr); no blast metric in either repo moves for it, so
the taint costs nothing measured.

## Counterexamples attempted

The 44-case gallery, each run against the prototype; every decline case
declines and every fold case folds to the expected names.

1. The celery shape, and why it is the sharpest. celery/local.py's
   LazyModule reads `self._object_origins[name]`, where the class body
   binds `_object_origins = {}` and the real mapping arrives at runtime:
   celery/__init__.py passes a literal by_module dict to recreate_module,
   which runs it through get_origins (a dict comprehension inside a helper,
   over a parameter), builds a class via type() with the result injected as
   a class attribute, and instantiates it into sys.modules. Every load-
   bearing fact crosses a function boundary, a dynamic class construction,
   and a module boundary. A folder that admitted class-attribute registries
   from their class-body bindings would fold these sites to the empty set,
   the exact vacuous-subset lie ADR 0009's accumulator counterexample
   documented, wearing class clothes: the local evidence says {} while
   runtime imports five modules. The rule that survives: class attributes
   are not module-scope bindings and never qualify; the celery sites
   decline as attribute receivers. Two of the three real lazy-init
   registry sites in the corpus are therefore unprovable by construction,
   and the honest statement is that this tier folds kombu, not "kombu and
   celery". (celery's second consumer, `self._direct[name]`, guards a dict
   that is empty at every call site in the repo; celery marks the branch
   pragma-no-cover. An omniscient folder would prove it imports nothing.
   This one declines all the same.)
2. The accumulator, through the back door. The transitive-expression
   extension let a name bound to a foldable expression deref into
   iteration, and the first corpus run promptly folded django's
   `candidates = ["django.templatetags"]` / `candidates.extend(...)` /
   `for candidate in candidates: import_module(candidate)` to the literal
   seed: ADR 0009's own sharpest counterexample, reproduced by the
   extension meant to generalize past it. A method mention is not a
   binding, so the census that guards expression bindings cannot see
   .extend. The rule that survives: expression bindings deref into
   iteration only for immutable shapes (tuple displays, and registry views
   whose receiver passes the mention scan); list and set displays and
   comprehension results reject however literal they look. Value-position
   folding was never exposed, because a list display folds as nothing in a
   string position. Same provenance as 0009's mutable-display rule: found
   as a live unsound fold in the survey, not predicted.
3. The conftest module surgery. `setattr(sys.modules[parent], attr,
   module)` with computed parent and attr, from kombu's own fixtures, as
   above: statically able to rebind any attribute of any module, including
   a registry. Resolved by mutator tainting, measured both ways.
4. STATICA_HACK, as above: an unknown-key globals store in the same module
   as the flagship fold. Resolved by the poison ladder; the ladder's
   value-type refinement is load-bearing, since a flat rule zeroes the
   measured win of this ADR and retroactively kills published v1 folds in
   kombu, requests, and pygments.
5. `globals()[computed] = "pkg.evil"`: the string-valued variant of the
   same store. No refinement survives it; every module-scope read in the
   module poisons, including anchors and provenance.
6. Registration functions. `def register(name, mod): REG[name] = mod` is
   the actual plugin-registry pattern, and the reason the wild registries
   the census scored are dynamic on principle: the store value is a
   parameter, so the registry declines at that store. The grammar draws
   the line exactly between "the module enumerates its own table" and "the
   world writes into the table".
7. Values built by subscripting the loop variable: `for pair in PAIRS:
   REG[pair[0]] = pair[1]`. Constant-index subscripts on loop variables are
   provable in principle but bought nothing in this corpus; they decline,
   and the tuple-target spelling of the same loop folds.
8. Swapped positions: `for k, v in PAIRS: REG[v] = k` folds to the key-
   position literals, because the folder folds the expression actually
   stored rather than assuming pairs feed values. An implementation that
   hard-wires "second position is the value" produces the subset lie here;
   the gallery pins the behavior.
9. Aliased accumulation with a rebound alias: `alias = REG` then `alias =
   {}`. The alias joins the family only with a single binding; a rebound
   alias rejects the registry, because stores through it before the
   rebinding still hit the shared dict.
10. Augmented stores, del, update from parameters, bare-argument escapes:
   all decline, same reasons as ADR 0009's registry rule, now with the
   store forms distinguished so the decline names the first offending
   mention.
11. Self-referential derivation: `REG[k] = REG[k]` cycles through the
    registry's own value set; the active-set guard declines it instead of
    recursing.
12. The walrus in the call: `__import__(REG[name], (name := f()) and None,
    None, [name])` breaks the left-to-right coupling argument; it declines
    twice over (the rebinding poisons the name's census, and the coupling
    check rejects any call whose argument tree contains a walrus of the
    key).
13. Tuple targets over string displays and over dicts (the unpack
    conflation), kept as regression guards.

## Interactions

### Narrowing and the observer regions

Folding a registry only adds DYNAMIC_IMPORT edges and removes a taint.
Narrowing's semantic closure subtracts INIT_REEXPORT edges only, so folded
edges always stay in the semantic closure and impact applies to them
unconditionally; a registry fold can therefore never widen what narrowing
excuses, only shrink the import-time-only region by making closures
bigger. The other direction is the one worth checking: de-tainting a
registry module lets closures that contain it become narrowable at all
(a tainted closure refuses narrowing today). Could a narrowed skip then
excuse a change that reaches a test only through the registry's lazy
imports? No, structurally: a registry module contains module-level loops,
subscript stores, and (in kombu) sys.modules stores and a class statement
with calls, every one of which rejects under the import-inertness
whitelist, so a registry module in the import-time-only region trips
condition 7 (non-inert observer) and the narrowing declines. The two
resolvers compose by both failing closed on each other's machinery, and
the fixture suite should pin that with a repo carrying both idioms.

### Placement, caching, and what purity survives

Recognition, the mention scan, the poison scan, and the value union are
per-blob: a pure function of one module's bytes and identities, cacheable
in ModuleFacts exactly like v1 folds, with the site's dependency names
(the registry family plus every module-scope name and anchor the fold
read) recorded alongside. The external-mention obligation is not per-blob
and cannot be: it joins one module's dependency names against every other
module's recorded mutating mentions at graph construction. That is a real
departure from ADR 0009's one-blob-one-fold property and the design owns
it: the join consumes only facts already in hand (the resolver seam's
ResolveContext carries the full facts mapping for exactly this shape of
proof), it stays per-revision (no relational claim, no witness fields, no
second snapshot), and replay re-derives it from the recorded revision the
same way it re-derives the graph. The cost is cache-shaped, not
soundness-shaped: a fold can be invalidated by a change in a different
file's mutation mentions, so folded edges are a construction-time product,
not a parse-time one.

### The soundness oracle and the mutation arm

Unchanged from ADR 0009, with one addition each way. The oracle's
runtime-import-subset assertion covers the derived fold directly, and the
fromlist coupling makes a specific observable claim (every __getattr__
import lands inside the folded edges plus pairs) that the import-log probe
tests without any outcome diffing. The mutation arm gains one derived-
specific probe: mutate the registry itself (add an entry with a sentinel
module, flip a value to a sibling module name) and assert the fold
recomputes and the new target's tests select; and one adversarial probe:
inject a mutating mention into a previously clean module and assert the
mutator acquires the taint.

## Measured rates from the census corpus

Prototype (throwaway, forked from the 0009 prototype): the extended folder
above plus the external-mention scanner, run over the same census clones
through acquit's snapshot pipeline, nothing executed. 60 of the 61 clones
analyzed; the one exclusion is sympy, whose snapshot fails in the
prototype's AST pass with a RecursionError, the same exclusion the census
and ADR 0009's measurement carried, and no other clone failed in any run.
The site universe is the prototype's own detector, which reproduces the
census population (579 sites, 516 of them test-reachable, across 42
repos), so the numbers compare directly with ADR 0009's published table.
The production tree measured against carries the in-flight v1 folding
implementation, and the v1 column
reproduces the published prototype's folds exactly (27 sites). Every
number below is measured from this run, not projected.

| Mode | Foldable sites (measured) | Files fully de-suspected (measured) |
| --- | --- | --- |
| v1 (ADR 0009 as published) | 27 | 9 |
| v1 plus corrections | 27 | 9 |
| derived registries | 28 | 10 |

The one new fold over the corrected baseline is the flagship itself:
kombu/__init__.py line 77, `__import__(object_origins[name], None, None,
[name])`, previously declined at the registry's subscript store, folded to
the eight `all_by_module` values plus the 120 coupled from-import pairs,
cap-exact at 128. It is the only site among the 579 that the derived
grammar recovers, and the one new de-suspected file is its module. The
items()-unpack, nested-loop, and transitive-constant extensions recover
nothing anywhere else: every other loop-built store in the corpus feeds
from parameters, attributes, or mutable accumulators that the grammar must
decline.

The parametric residue stays parametric. Of the 441 sites ADR 0009
classified as parameter- or attribute-fed, zero fold under the derived
grammar. The registries that feed the wild `symbol_by_name`
and plugin-loader patterns live in different modules from their consumers
(pygments' _mapping files, celery's loader tables), arrive through
function parameters, or are populated by exactly the registration
functions the grammar must decline; the derived tier proves the module
that builds its own table in place, and almost nothing else. The census
scored the idiom's exposure; ADR 0009 measured v1's recoverability; this
measures the next tier's, and the gap barely moves.

Blast radius, the number that matters:

| Blast measure (measured) | Before | v1 plus corrections | Derived |
| --- | --- | --- | --- |
| median R007 blast radius, 41 repos with tests and sites | 100% | 100% | 100% |
| median overall taint blast radius | 100% | 100% | 100% |
| repos moving any blast metric vs the unfolded baseline | | 3 (fastapi, pipx, nox: v1's own published folds) | the same 3 |
| repos moving any blast metric vs v1 plus corrections | | | 0 of 60 |

kombu deserves its honest paragraph, because it is the repo this tier was
named for. The derived fold proves the flagship site, de-taints
kombu/__init__.py (the fat init sitting in 100 percent of closures), and
moves no selection number at all: every one of kombu's 62 collected test
files also reaches t/unit/conftest.py, which carries six dynamic-import
sites of the module-mocking kind no sound fold can bound, so R007 pins
everything before and after. The win the census score promised was already
spent by the repo's own test infrastructure. celery moves nothing for the
prior reason (its two sites are unprovable by construction). The derived
tier's measured selection delta over the whole corpus is
zero: no repo's R007 or overall taint blast moves between v1 plus
corrections and the derived tier, and the only three repos that move at
all against the unfolded baseline (fastapi, pipx, nox) are ADR 0009's own
published wins, untouched by this tier.

## Expected win, quantified

Against ADR 0009's baseline the honest statement is: the derived-registry
grammar as specified proves one additional site in one repo, fully
de-suspects one additional file, and changes median blast
radius in no repo. The two named flagship repos split: kombu folds and
gains nothing (its conftest pins everything), celery cannot fold (its
registry is assembled interprocedurally). The corrections the hunt forced
on v1 are the durable output: the globals-store poison closes a live hole
in the rules the production folder ships today, the fromlist
resolution correction prevents the derived tier's own edges from reading
as broken, and the external-mutation obligation names an assumption every
module-state fold silently makes and gives it the same checked-or-tainted
treatment ADR 0008's condition 7 gave observer regions.

The narrowing campaign supplies the discount rate for reading numbers
like these. Re-export narrowing measured 6.7 percent static applicability
on the repos whose inits actually re-export, then produced zero narrowed
skips on 259 real merged PRs across flask, rich, httpx, black, and
uvicorn: real diffs must thread every intersecting file through every
condition at once, and one failure anywhere voids the skip. Static site counts are a
ceiling, and the measured ratio between that ceiling and real-PR yield is
now on record as effectively infinite. This tier starts from a lower
ceiling than narrowing did (one repo, selection delta already zero at the
static level, the joint condition already failed by kombu's own conftest
before any PR is drawn), so no discount arithmetic can rescue it.

## Prototype provenance

The prototype lives outside the repository and is disposable by design: a
forked folder (~1700 lines) with the three modes measured side by side, a
survey harness reusing acquit's snapshot pipeline, an external-mention
scanner, and a 44-case adversarial gallery in which every counterexample
above declines and every admitted idiom folds to the expected names. Rates
are from a single run against the census clones at their 2026-08 HEADs.
The kombu walkthrough (8 values, 120 coupled pairs, cap-exact at 128) was
verified by hand against the clone. Nothing from the prototype ships; if
the build decision were ever revisited, the folder would be rebuilt inside
graph/resolvers/ with the gallery promoted to fixtures.
