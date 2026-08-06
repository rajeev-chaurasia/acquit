# M1 mini-study: acquit on click's last 30 substantive commits

- Repo under test: pallets/click (scratchpad clone), analyzed 30 most recent non-merge, non-version-bump commits on `main`.
- Skipped as version-bump-only: 1 (b2e30a175 Release version 8.4.2).
- Unit of selection: test files (click's suite is 21-34 test files across the window).
- Timing includes `uv run` process startup; measured baseline overhead ~0.62s per invocation.

## Headline numbers (baseline, out-of-the-box acquit)

- Commits analyzed: **30**
- Selective: **8/30** (26.7%); run-all: 22/30
  - run-all forced by a global fail-closed finding: 18
  - run-all because the diff genuinely reaches every test file (no global finding): 4
- Skip fraction on selective commits (skipped/total test files): median **88.2%**, min 60.6%, max 97.1%
- Suite-level: 203 of 846 test-file executions across the 30-commit window skipped (24.0%)
- Analysis wall time: mean 4.62s, p95 5.05s per commit (subtract ~0.62s uv startup for tool-internal time)
- Replay verification: **8/8 verified, 0 mismatches**

## Per-commit results (baseline)

| sha | subject | files | mode | sel | skip | total | replay | secs |
|---|---|---|---|---|---|---|---|---|
| 0f4738df8 | Fix docs and changelog | 11 | run-all | 0 | 0 | 0 | - | 5.54 |
| 0c9e836c7 | Update all GitHub actions, fix thread locking | 6 | run-all | 0 | 0 | 0 | - | 5.01 |
| 10b43c211 | Parametrize deprecation tests and check targets | 1 | selective | 1 | 33 | 34 | verified | 4.67 |
| 3b16957cb | Make `prompt`/`ParamType` typing work without ru | 9 | run-all | 0 | 0 | 0 | - | 4.95 |
| 0585f456b | `click.prompt` typing clarifications / improveme | 6 | run-all | 0 | 0 | 0 | - | 4.67 |
| c2ed41490 | Deprecate `isolated_filesystem` and document its | 10 | run-all | 0 | 0 | 0 | - | 4.67 |
| 051725fa7 | Add tests to deprecations. Better deprecate stre | 5 | run-all | 0 | 0 | 0 | - | 4.60 |
| 7a0a3447f | Mark clearly functions private status. Deprecate | 13 | run-all | 0 | 0 | 0 | - | 4.80 |
| b3a191bf5 | Fix sdist include to ship CHANGES.md after chang | 1 | run-all | 0 | 0 | 0 | - | 4.75 |
| 8f300853d | Streamline option flag management | 2 | run-all | 0 | 0 | 0 | - | 4.87 |
| 71f2bafa5 | Strip all ANSI sequences | 5 | run-all | 0 | 0 | 0 | - | 4.75 |
| 3495fba16 | Warn when an argument name collide with other pa | 6 | run-all | 0 | 0 | 0 | - | 5.05 |
| 07c909f23 | Validate style() color arguments, fix explicit b | 3 | run-all | 0 | 0 | 0 | - | 4.77 |
| c52f43c8b | Restore `test_echo_color_flag` from #3505 that w | 1 | selective | 1 | 32 | 33 | verified | 4.85 |
| 47cc96fb1 | Add files for _detect_program_name, _expand_args | 15 | selective | 13 | 20 | 33 | verified | 8.94 |
| 700798252 | Add KeepOpenFile, LazyFile and open_file files. | 4 | selective | 4 | 27 | 31 | verified | 4.86 |
| 18400b249 | Add format_filename and sentinel files. | 3 | selective | 3 | 25 | 28 | verified | 4.60 |
| 1103c5cac | Move move test to prompt file. | 2 | selective | 2 | 24 | 26 | verified | 4.34 |
| 182944f90 | Make confirm, prompt, style files. | 4 | selective | 4 | 22 | 26 | verified | 4.01 |
| a391797d0 | Make test echo file. | 4 | selective | 3 | 20 | 23 | verified | 4.02 |
| 13f075c4b | Update documentation following Colorama removal | 3 | run-all | 0 | 0 | 0 | - | 4.00 |
| 445310365 | Use `click.echo` everywhere in the docs, documen | 1 | run-all | 0 | 0 | 0 | - | 4.00 |
| 9f9b149ea | Add `@custom_version_option`, freeze `@version_o | 5 | run-all | 0 | 0 | 0 | - | 3.87 |
| 30749b45c | Don't parameterize tests using non-Collection it | 2 | run-all | 0 | 0 | 0 | - | 4.13 |
| 93ba0075f | Strip ANSI from `confirm()` and `prompt()` when  | 3 | run-all | 0 | 0 | 0 | - | 3.96 |
| 6f85d26dc | Do not trigger a `BytesWarning` under `python -b | 2 | run-all | 21 | 0 | 21 | - | 4.01 |
| 1ac08db95 | Fix ruff E501 line-too-long in PowerShell templa | 1 | run-all | 21 | 0 | 21 | - | 3.85 |
| 27b3ee263 | Add built-in PowerShell shell completion support | 4 | run-all | 0 | 0 | 0 | - | 4.19 |
| c4a6b57bb | type-hint out varibale | 1 | run-all | 21 | 0 | 21 | - | 3.97 |
| 748a34d01 | optimize split_arg_string with extend(...) inste | 1 | run-all | 21 | 0 | 21 | - | 4.04 |

## Fail-closed rule histogram (global findings that forced run-all)

| rule | commits forced | triggering files (count) | classification |
|---|---|---|---|
| R001 | 16 | CHANGES.md (x13), docs/utils.md (x4), docs/documentation.md (x2), docs/shell-completion.md (x2), docs/handling-files.md (x2), docs/arguments.md (x1), docs/option-decorators.md (x1), docs/testing.md (x1), docs/upgrade-guides.md (x1), docs/api.md (x1) | addressable |
| R002 | 3 | pyproject.toml (x3), uv.lock (x2) | inherent |
| R003 | 1 | .github/workflows/lock.yaml (x1), .github/workflows/nightly.yaml (x1), .github/workflows/pre-commit.yaml (x1), .github/workflows/publish.yaml (x1), .github/workflows/tests.yaml (x1), .github/workflows/zizmor.yaml (x1) | addressable |
| (none: full graph impact) | 4 | core `src/click/*.py` modules reachable from every test file | inherent to file-level import graphs |

Non-global findings (scoped: they pin individual tests to the run but never forced run-all in this window):
- R007: on 30/30 commits; 1 distinct files (top: examples/complex/complex/cli.py (x30)): non-literal dynamic import; head-tree property, fires on every commit, taints only the examples/ closure
- R014: on 21/30 commits; 36 distinct files (top: tests/test_utils.py (x6), tests/test_basic.py (x4), tests/test_deprecations.py (x3)): changed test file must run itself; diff-dependent and correctly scoped

## Run-all reason classification

- **Addressable** (rule/policy change could recover selectivity):
  - R001 on `CHANGES.md` / `docs/*.md`: docs and changelog edits ride along with almost every PR; these files feed no test.
  - R003 on `.github/workflows/*.yaml`: CI workflow config cannot change the behavior of a local pytest run.
- **Inherent** (correctly conservative at this analysis granularity):
  - R002 on `pyproject.toml` / `uv.lock`: dependency environment may change.
  - Full-impact run-alls: every click test imports `click`, and `click/__init__.py` re-exports from every core module, so any edit to `core.py`, `termui.py`, `shell_completion.py`, `_compat.py`, `utils.py`, ... reaches 100% of test files. No fail-closed rule is involved; only symbol-level (not file-level) analysis could shrink these.

## Counterfactuals (re-run of the same 30 commits)

| variant | selective | median skip (selective) | suite-level skipped | replay |
|---|---|---|---|---|
| baseline | 8/30 | 88.2% | 203/846 (24.0%) | 8/8 verified, 0 mismatch |
| CF1: `assume_inert` docs/changelog (R001 relaxation) | 10/30 | 90.8% | 244/846 (28.8%) | 10/10 verified, 0 mismatch |
| CF2: CF1 + waive R003 for `.github/workflows/*` | 11/30 | 92.3% | 278/846 (32.9%) | 11/11 verified, 0 mismatch |

- CF1 flips 2 commits to selective: 445310365 (Use `click.echo` everywhere in the docs,); 30749b45c (Don't parameterize tests using non-Colle).
- CF2 additionally flips 1: 0c9e836c7 (Update all GitHub actions, fix thread lo).
- Every other run-all survives the counterfactuals because the diff includes a core `src/click` module (full impact) or a dependency manifest (R002).

## Verdict against the M1 tripwire

- Median selective-commit skip fraction: 88.2% baseline (threshold: >=20%). **Pass, by a wide margin.**
- Share of commits selective at all: 26.7% baseline, 36.7% after two small policy changes. This is the weak axis, and its ceiling on click is ~37%: 16 of the 19 remaining run-alls are genuine full-graph impact (click's `__init__.py` re-exports every core module, so every test file reaches every module), which no fail-closed rule change can recover. Only symbol-level analysis would move those.
- Zero replay mismatches across 29 selective runs (8 baseline + 10 CF1 + 11 CF2): the witness/replay contract held everywhere.
- Single most valuable rule change: make R001 treat documentation resources (`docs/**`, `*.md`, `*.rst`, `CHANGES*`) as inert by default (or scope R001 to resources actually referenced under a test root). R001 fired globally in 16 of the 18 finding-driven run-alls (in 14 it was the only global rule); relaxing it flips 2 commits outright, is a precondition for most future flips (changelog edits ride along with nearly every PR), and raises the median skip to 90.8%.

