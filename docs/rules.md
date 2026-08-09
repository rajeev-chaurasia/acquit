# Rule reference: R001 to R018

Every one of these rules turns something the static analysis cannot bound
into a finding, and every finding widens the test run. Rules never narrow
anything. All rules are evaluated on every run, the engine never
short-circuits, and every finding appears in the report with the exact reason
text quoted below. The source of truth is `src/acquit/policy/rules.py`; the
contract they enforce is [soundness.md](soundness.md), and the design
rationale is [ADR 0001](adr/0001-fail-closed-policy.md).

Reason texts are templates: `{path}` and friends are filled with the real
subject at run time.

## Scopes

| Scope | Effect on selection |
| --- | --- |
| global | Nothing may be skipped. |
| global-if-reached | Acts like global once any test can reach the subject module through the head graph; with no reaching test the finding stays in the report but has no selection effect. |
| subtree | Every test under a directory must run. |
| closure-taint | Every test whose import closure reaches the tainted file must run. |
| self-test | The changed test itself always runs. |

## R001: changed resource file

- Trigger: a changed file the analysis cannot classify as anything it
  models. Concretely, anything that is not a Python module, test, conftest,
  `.pyi` stub, or a recognized config file is a resource, so docs, data
  files, and changelogs all land here. The built-in inert list is empty;
  only your own `assume_inert` globs exempt a path.
- Scope: global.
- Reason: `{path} is a resource file, and any test could read it at runtime.`
- Example: a PR edits `CHANGES.rst`. Nothing proves no test opens that file,
  so the whole suite runs. If your tests never read it,
  `assume_inert = ["CHANGES*"]` under `[tool.acquit]` lifts this rule for
  that path, and the proof obligation becomes yours.

## R002: changed dependency manifest

- Trigger: a changed file named `pyproject.toml`, `setup.py`, `setup.cfg`,
  `Pipfile`, `Pipfile.lock`, `uv.lock`, or `poetry.lock`, or matching
  `requirements*.txt` or `constraints*.txt`.
- Scope: global.
- Reason: `{path} is a dependency manifest, so the environment of every test may change.`
- Example: a bot bumps `uv.lock`. Third-party code is treated as constant
  only while the manifests are unchanged (assumption A2 in soundness.md), so
  a lockfile diff runs everything.

## R003: changed test environment

- Trigger: a changed root `conftest.py`, anything under
  `.github/workflows/`, `pytest.ini`, `tox.ini`, `sitecustomize.py`,
  `usercustomize.py`, any `*.pth` file, or any `Dockerfile*`.
- Scope: global.
- Reason: `{path} shapes the test environment, so every test could be affected.`
- Example: a PR tweaks `.github/workflows/tests.yml`. The workflow decides
  how tests run, which is outside what an import graph can bound.

## R004: changed native or build source

- Trigger: a changed file named `CMakeLists.txt`, `Makefile`, or
  `meson.build`, or ending in `.c`, `.h`, `.cc`, `.cpp`, `.hpp`, `.pyx`,
  `.pxd`, `.so`, or `.pyd`.
- Scope: global.
- Reason: `{path} is native build input that the import graph cannot see through.`
- Example: a PR edits `src/speedups.c`. The compiled extension it produces
  can be imported by anything, and no Python import statement records that
  dependency.

## R005: changed non-root conftest

- Trigger: a changed `conftest.py` anywhere other than the repository root
  (the root conftest is R003, global).
- Scope: subtree, the conftest's directory.
- Reason: `{path} changed, and pytest applies it to every test under {directory}/.`
- Example: `tests/api/conftest.py` changes a fixture. Every test under
  `tests/api/` runs; tests elsewhere are unaffected by this finding.

## R006: collection-altering hook or unresolvable pytest plugin

- Trigger: any conftest that defines a name that can change what pytest
  collects (`pytest_collect_file`, `pytest_ignore_collect`,
  `pytest_pycollect_makemodule`, `collect_ignore`, `collect_ignore_glob`),
  any conftest that cannot be parsed, or a `pytest_plugins` entry (in a
  conftest or a test module) that looks first-party but does not resolve.
  Note this rule fires on presence, not on change: an untrustworthy
  collection makes every selection untrustworthy.
- Scope: global.
- Reasons, one per case:
  - `{path} could not be parsed, so its effect on collection is unknown.`
  - `{path} defines {names}, which can change which tests pytest collects.`
  - `{path} declares pytest plugin {entry!r}, which looks first-party but does not resolve.`
- Example: a conftest defines `pytest_ignore_collect`. Static discovery can
  no longer predict which files pytest collects, so no skip list derived
  from that discovery can be trusted.

## R007: non-literal dynamic import

- Trigger: a file that imports a module chosen at runtime: non-literal
  `importlib` calls, non-literal `__import__`, or non-literal `sys.modules`
  access. Literal dynamic imports (a quoted module name) are resolved into
  ordinary edges instead.
- Scope: closure-taint.
- Reason: `{path} imports a module chosen at runtime, so its true dependencies are unknown.`
- Example: `plugins.py` calls `importlib.import_module(name)` on a computed
  string. Every test whose closure reaches `plugins.py` always runs.

## R008: sys.path mutation

- Trigger: any mutation of `sys.path`, scoped by when the mutation executes.
- Scope and reasons, three cases:
  - Import-time (module level or class body) in a conftest: global.
    `{path} mutates sys.path at import time, and conftests execute unconditionally during collection.`
  - Import-time in a plain module: global-if-reached.
    `{path} mutates sys.path at import time, which perturbs every later import in the process, but only if something imports this module during the test session.`
  - Function-level anywhere: closure-taint.
    `{path} mutates sys.path inside a function, making its own dynamic behavior unknowable at runtime.`
- Example: a module does `sys.path.insert(0, ...)` at the top level. If any
  test imports it, every later import in the process may resolve
  differently, so the run goes global; if nothing reaches it, the finding is
  recorded and changes nothing. The three-way split was refined by the
  replay study; the story is in [docs/study.md](study.md).

## R009: exec, eval, or compile

- Trigger: a file that calls `exec`, `eval`, or `compile`.
- Scope: closure-taint.
- Reason: `{path} calls exec, eval, or compile, so its true dependencies are unknown.`
- Example: a settings loader `exec`s a config snippet. The executed code can
  import anything, so every test reaching the loader runs.

## R010: unparseable file

- Trigger: a Python file that fails to parse, or a listed file whose
  contents cannot be read (a submodule entry, say).
- Scope: closure-taint.
- Reason: `{path} could not be parsed, so its imports are unknown.`
- Example: a file uses syntax newer than the analyzing interpreter. Its
  imports are unknowable, so it taints every test that reaches it.

## R011: broken first-party import

- Trigger: a changed file with an import that looks first-party but does not
  resolve to any module. (Any file with such an import is tainted in the
  graph regardless; this rule additionally surfaces the changed ones in the
  report, one finding per broken name.)
- Scope: closure-taint.
- Reason: `{path} imports {name!r}, which looks first-party but does not resolve to any module.`
- Example: a refactor renames `pkg/helpers.py` but a changed file still does
  `from pkg import helpers`. The dependency cannot be modeled, so dependent
  tests run.

## R012: opaque module __getattr__

- Trigger: a module-level `__getattr__` assignment that can import lazily. A
  `def`-form `__getattr__` is bounded by its own body and does not trigger
  this rule; its body's imports become ordinary edges.
- Scope: closure-taint.
- Reason: `{path} defines a module __getattr__ that can import lazily, so its true dependencies are unknown.`
- Example: `__getattr__ = _lazy_loader` at module level. Attribute access
  can import anything, so the module's true dependencies are unknowable.

## R013: changed .pyi stub

- Trigger: a changed `.pyi` file.
- Scope and reasons, two cases:
  - With a sibling `.py` module: closure-taint on the sibling.
    `{path} is the stub for {sibling}, so its dependents may be affected.`
  - Orphaned (no sibling module): global.
    `{path} is a stub with no sibling module, so its reach cannot be modeled.`
- Example: `pkg/core.pyi` changes. Everything depending on `pkg/core.py`
  runs; a stub with no sibling could describe anything, so it runs the
  world.

## R014: changed test or conftest file

- Trigger: a changed file that is itself a test file or a conftest.
- Scope and reasons, two cases:
  - A test file: self-test.
    `{path} is a test file and must run because it changed.`
  - A conftest: subtree of its directory, or global for the root conftest.
    `{path} is a conftest, so the tests it configures must run.`
- Example: you edit `tests/test_cli.py`. That file always runs, whatever the
  graph says about it.

## R015: --doctest-modules configured

- Trigger: the pytest configuration enables `--doctest-modules`, which makes
  tests live inside source modules, beyond static test discovery. Fires on
  presence, not on change.
- Scope: global.
- Reason: `{source} enables --doctest-modules, so doctests execute inside source modules and static test discovery cannot bound them yet.`
- Example: `addopts = --doctest-modules` in `pyproject.toml`. Every module
  is potentially a test, so no file-level skip list is meaningful.

## R016: diff or base ref unavailable

- Trigger: the diff or the base ref cannot be obtained, most commonly a
  shallow clone that never fetched the base.
- Scope: global.
- How it surfaces: R016 reserves the rule id in the table, but in the
  current CLI this failure travels the fail-closed error path instead of the
  policy engine: `select` prints
  `acquit: cannot resolve the requested refs, running all tests: {detail}`,
  writes a run-all selection plus a report whose finding carries R018, and
  exits 3. Either way the answer is the same: run everything.

## R017: corrupt or version-mismatched cache

- Trigger: a parse-cache entry that is unreadable, corrupt, or written by a
  different cache format version.
- Scope: rebuild. This is the one rule with no selection effect and no
  finding in the report: every cache failure silently degrades to a full
  re-parse, because a broken cache must never break (or bias) analysis. The
  cache is content-addressed by git blob sha and lives outside the checkout;
  see the limitations section of [soundness.md](soundness.md) for the trust
  boundary.

## R018: internal error

- Trigger: any unexpected error anywhere in a `select` run.
- Scope: global.
- Reason: the error's own message (or its type name when the message is
  empty), with subject `acquit`. `R018:acquit` is also the fallback
  attribution when a test is forced to run by graph taint and no specific
  finding claims it: taint is never silently dropped.
- Example: a bug in acquit raises mid-analysis. The CLI writes a run-all
  selection and a report carrying the R018 finding, prints
  `acquit: internal error, run all tests: ...`, and exits 3. A crash and a
  hostile input converge on the same safe answer.

## Deliberate non-triggers

These look dynamic but are already covered by over-approximation, so no rule
fires:

- Imports inside `try/except ImportError` blocks: collected as edges
  unconditionally.
- Imports inside `TYPE_CHECKING` blocks: edges.
- Imports under platform conditionals (`if sys.platform == ...`): every
  branch contributes edges.
- Imports inside function bodies: edges, which is exactly why R012 could be
  narrowed to the opaque assignment form.
- Attribute monkeypatching in tests: it does not change the import graph,
  and the patched module is already in the test's closure.

False positives from over-approximation (running an unaffected test) are
expected and harmless; the direction of every error is the point.

## Waivers

`[tool.acquit.waive]` entries (a rule, a glob, and a mandatory justification)
shift the proof obligation for matching findings to you. Waived findings stay
in the report. Treat `.acquit.toml` diffs as security-relevant in review; the
reasons are spelled out in [soundness.md](soundness.md).
