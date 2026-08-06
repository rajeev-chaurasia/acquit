# ADR 0002: Static import analysis instead of runtime coverage

Status: accepted

## Context

The established way to select tests is runtime coverage tracing
(pytest-testmon and the services built on it): run the suite once under
tracing, store which lines each test touched, then map diffs to tests. It is
precise, but it requires executing the code, keeping a coverage database
fresh across CI runs and machines, and trusting that the traced run
represents the run being skipped.

## Decision

Acquit never executes user code. The dependency graph comes from parsing
import statements, pytest configuration, and conftest scoping. Selection is a
pure function of (tree, diff): stateless, deterministic, and reproducible on
any machine, which is what makes witnesses re-checkable after the fact.

## Consequences

- No database to persist, invalidate, or trust; nothing to warm up.
- Analysis of hostile or broken code is safe by construction.
- Granularity is the test file, not the test function, because module-level
  imports are the unit static analysis can bound honestly.
- Dynamic behavior cannot be traced, so it fails closed via the ADR 0001 rule
  table instead of being silently mis-modeled.
