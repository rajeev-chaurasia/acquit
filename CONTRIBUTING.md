# Contributing

Thanks for looking under the hood. Acquit is small on purpose, and the bar
for changes scales with how close they sit to the soundness contract.

## Dev setup

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), and git.

```
git clone https://github.com/rajeev-chaurasia/acquit.git
cd acquit
uv sync
```

`uv sync` creates the venv and installs the dev group. The gate that CI runs
on every PR, in order:

```
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python scripts/check_style.py
uv run pytest --cov
```

Run all five before pushing; they are cheap and the CI matrix (Linux, macOS,
Windows, on 3.12 and 3.13) runs exactly these.

## House rules the gate enforces

- Ruff for linting and formatting, config in `pyproject.toml`.
- mypy strict over `src/`; new code ships typed.
- No em-dashes anywhere, in code, docs, or config
  (`scripts/check_style.py` fails the build on one).
- Determinism: the same tree and diff must produce byte-identical documents,
  apart from `created_at`. Sort anything you iterate, never depend on dict
  or filesystem ordering, and keep randomness out of the analysis path.
- The analysis never executes user code. Not "sandboxed", never: parsing
  only.
- Every degraded path converges on "run all tests". If you add a failure
  mode, its failure must widen the run, loudly.

## Running the tests

```
uv run pytest              # the full suite, including the oracle
uv run pytest -m oracle    # just the runtime soundness oracle
```

The suite has four tiers under `tests/`: `unit`, `property` (hypothesis),
`adversarial` (committed attacks on the analysis and the delivery layer),
and `integration`, which includes the soundness oracle. The oracle builds
fixture repos, runs real pytest in subprocesses under an import recorder,
and asserts that every runtime-observed first-party import edge is inside
the static closure; it needs git on PATH.

The replay study is runnable too; see [study/README.md](study/README.md) for
manifests, sharding, and aggregation. Aggregation fails on any unsafe skip.

## The bar for soundness-critical changes

Soundness-critical code is anything a wrong answer can turn into an unsafe
skip: the policy rules and engine, graph assembly and resolution, selection
and witnesses, replay, the tree fingerprint, the pytest plugin, and the
action's shell steps. A change to a rule (adding one, narrowing one, or
changing a scope) needs all three:

1. A fixture that trips it: a test repo under `tests/` where the rule fires,
   and where the narrowed rule still fires on what it must still catch.
2. An adversarial argument: a written case for why the change cannot produce
   an unsafe skip, expressed as a test in `tests/adversarial/` when it can
   be. If you are narrowing a rule, the reproduction that motivated the
   original width must still pass unchanged.
3. A study re-run for precision claims: if the change is justified by "this
   makes acquit more selective", the claim needs numbers from the replay
   study, not intuition. The R008 story in [docs/study.md](docs/study.md) is
   the template: the first safe fix was uselessly wide, and only the study
   showed it.

Findings, reason texts, and document schemas are user-facing contracts;
changes to them should update [docs/rules.md](docs/rules.md) and
[docs/cli.md](docs/cli.md) in the same PR, and schema changes bump the
schema name (`selection-v2` exists because v1 was demonstrated unsafe).

## Commits and PRs

- Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`, `ci:`,
  with an optional scope like `feat(study):`. Release automation reads
  these.
- PRs are squash-merged, so the PR title must itself be a valid conventional
  commit line; it becomes the commit on main.
- Keep commits self-contained: the gate should pass at every commit that
  lands on main.

## Reporting an unsafe skip

If you can construct a diff where acquit skips a test the change actually
affects, that is the most valuable issue you can file. Use the unsafe-skip
issue template and see [SECURITY.md](SECURITY.md): confirmed unsafe skips
are treated as release blockers.
