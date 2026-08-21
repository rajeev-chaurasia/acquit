# ADR 0001: Fail-closed policy semantics

Status: accepted

## Context

Acquit decides which pytest test files a change cannot affect. A wrong skip is
worse than a thousand wasted test runs, so the burden of proof sits entirely on
skipping: the default answer is always "run everything", and a skip requires an
affirmative, machine-checkable witness. Static import analysis cannot bound
every Python behavior, so the boundary must be explicit, enumerated, and tested.

## Decision

A fixed, ordered rule table (R001 to R018) turns everything the analysis cannot
bound into findings. Every rule is evaluated on every run; the engine never
short-circuits, so reports list every reason. Scopes:

- global: nothing may be skipped
- global-if-reached: nothing may be skipped once any test can reach the
  subject module through the head graph; with no reaching test the finding
  stays in the report but has no selection effect
- subtree: every test under a directory must run
- closure-taint: every test that can reach a tainted node must run
- self-test: a changed test always runs

| Rule | Trigger | Scope |
|---|---|---|
| R001 | changed file we cannot classify as analyzable (any non-Python file; the built-in inert list is empty) | global |
| R002 | changed dependency or environment manifest (pyproject.toml, lockfiles, requirements) | global |
| R003 | changed test environment (pytest.ini, tox.ini, workflows, root conftest, .pth, sitecustomize) | global |
| R004 | changed native or build source (C, C++, Cython, CMake, Makefile) | global |
| R005 | changed non-root conftest.py | subtree |
| R006 | conftest with collection-altering hooks or unresolvable first-party pytest_plugins | global |
| R007 | non-literal dynamic import (importlib, __import__, non-literal sys.modules access) | closure-taint |
| R008 | sys.path mutation (direct, site.addsitedir, pkgutil.extend_path, monkeypatch.syspath_prepend, pytester.syspathinsert) | import-time (module level or class body) in a conftest or changed plain module: global; import-time in an unchanged plain module: global-if-reached, the mutation leaks process-wide but only once something imports the module; function-level anywhere: closure-taint, it runs only if called |
| R009 | exec, eval, or compile | closure-taint |
| R010 | file that fails to parse | closure-taint |
| R011 | import that looks first-party but does not resolve | closure-taint |
| R012 | opaque module __getattr__ assignment (a def-form hook is bounded by its own body) | closure-taint |
| R013 | changed .pyi stub (couples to its sibling module; global when orphaned) | closure-taint |
| R014 | changed test or conftest file | self-test |
| R015 | --doctest-modules configured (doctests live inside source modules) | global |
| R016 | diff or base ref unavailable | global |
| R017 | corrupt or version-mismatched cache (silent full rebuild) | rebuild |
| R018 | any internal error | global |

Deliberate non-triggers, because over-approximation already covers them: imports
inside try/except ImportError, TYPE_CHECKING blocks, platform conditionals, and
function bodies are all collected as edges unconditionally; attribute
monkeypatching in tests does not change the import graph.

Waivers exist ([tool.acquit.waive] with rule, glob, and a mandatory
justification) and shift the proof obligation to the user. Waived findings stay
in the report.

## Consequences

Precision problems surface as loud run-all decisions with reasons, never as
silent unsafe skips. The reason histogram over real repositories becomes the
roadmap: the biggest bar is the next rule worth narrowing, the way R012 was
narrowed once function-body imports proved to already be edges.
