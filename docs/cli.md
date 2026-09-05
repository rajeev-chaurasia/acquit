# CLI reference

Everything the `acquit` command does, flag by flag. The one rule that governs
all of it: every failure path converges on "run all tests". A run-all answer
is a valid answer, not an error.

Install with `pip install acquit` (or `uv pip install acquit`). Run commands
from inside the repository you want analyzed; acquit resolves the enclosing
git repository from the current directory. `acquit --version` prints the
version and exits.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success. This includes a run-all decision: "nothing can be proven skippable" is a correct result, and `comment` and `ci-outputs` always exit 0 so delivery plumbing can never fail CI. |
| 2 | Usage error: bad arguments, an unusable document, or (for `explain`) a path that is not a known test file at head. |
| 3 | Internal error. `select` first writes fail-closed documents: a run-all selection and a report carrying an R018 finding, so whatever went wrong, pytest runs everything. |
| 4 | Replay mismatch: at least one witness or document failed re-verification. |

## acquit select

Decide which tests must run for a diff, and write the three output documents.

```
acquit select --base origin/main
acquit select --base origin/main --head HEAD
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--base` | required | Base git ref to diff against. |
| `--head` | working tree | Head git ref. Without it, select analyzes the working tree; the report then records no head sha and cannot be replayed, so pass `--head` on a real commit when the run must be replayable. |
| `--report` | `acquit-report.json` | Report output path. |
| `--selection` | `acquit-selection.json` | Selection output path. |
| `--witnesses` | `acquit-witnesses.json` | Witnesses output path. |
| `--durations` | none | JSON file mapping test paths to seconds; enables `estimated_seconds_saved` in the report's stats. |

Select prints a one-line summary (`acquit: selective: ...` or
`acquit: run-all: ...`) and exits 0. The default output paths land in the
current directory and are safe as they are: the selection records its own
outputs and they never invalidate the tree fingerprint. An unresolvable ref
is reported plainly as an operator mistake, fail-closed documents are
written, and the exit code is 3.

## acquit analyze

Build the dependency graph for the working tree and print its health as
canonical JSON: file count, node and edge counts, test count, tainted node
count, import roots, and the graph hash. No flags.

```
acquit analyze
```

## acquit explain

Walk through the decision for one test file.

```
acquit explain tests/test_units.py --base origin/main
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `test` | positional, required | Repo-relative test file path. |
| `--base` | required | Base git ref. |
| `--head` | working tree | Head git ref. |

The output names the decision and its evidence: for a skipped test, the
witness id, claim, closure size, and hash; for a selected test, every reason,
with the import chain spelled out for `reachable-from:` reasons; for an
always-run test, the finding that captured it; when a global finding forces
the full suite, that finding. A path that is not a known test file at head
exits 2.

## acquit replay

Re-verify the witnesses behind a report from first principles. Replay
rebuilds the snapshot at the report's recorded head sha with no cache,
recomputes every skipped test's import closure, and checks every witness and
every graph hash. This is what makes witnesses evidence rather than logs.

```
acquit replay acquit-report.json
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `report` | positional, required | Path to an acquit report file. |
| `--witnesses` | `acquit-witnesses.json` beside the report | Witnesses file path. |
| `--selection` | `acquit-selection.json` beside the report, when present | Selection file to cross-check against the report: graph hash and skip list must agree. |

Omitted document flags resolve beside the report, never against the current
directory. Exit 0 prints `replay ok: N witnesses verified`. Any mismatch
lists every failure and exits 4. An unreadable or mis-schemaed document, or a
report built from a working tree (no head sha), exits 2.

## acquit comment

Post or update the sticky PR comment for a report. Never fails CI: every
error is a warning on stderr and exit 0.

```
acquit comment acquit-report.json --pr 42
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `report` | positional, required | Path to an acquit report file. |
| `--pr` | inferred from `GITHUB_REF` | Pull request number. |

Requires `GITHUB_TOKEN` and `GITHUB_REPOSITORY` in the environment;
`GITHUB_API_URL` overrides the API endpoint for GitHub Enterprise. The
comment is upserted in place using a hidden marker, so each PR carries
exactly one acquit comment.

## acquit ci-outputs

Write GitHub Actions outputs and a step summary for a completed run. Never
fails CI: every error is a warning on stderr and exit 0.

```
acquit ci-outputs acquit-report.json acquit-selection.json
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `report` | positional, required | Path to an acquit report file. |
| `selection` | positional, required | Path to the selection file pytest will obey. |

Writes `mode`, `selected`, `skipped`, `always-run`, `total`, `report`, and
`selection` records to `GITHUB_OUTPUT` and a markdown digest to
`GITHUB_STEP_SUMMARY`. The mode comes from the selection document, not the
report: when replay rewrote the selection to run-all, the document pytest
obeys wins. Values that could break the runner-file format (newlines, the
heredoc delimiter) are refused before anything is written.

The replay study ships as a separate console script, `acquit-study`;
[study/README.md](../study/README.md) documents it.

## The three output documents

One `select` run writes three JSON documents, canonically serialized
(sorted keys, two-space indent), so identical inputs produce byte-identical
files apart from the `created_at` timestamp.

### `acquit/report-v1` (the report)

The full account of the run, for humans and for `comment`/`ci-outputs`:
tool version and graph schema, base and head shas, graph summary (hash,
node and edge counts, import roots), every changed file with its
classification and status, the decision mode with every policy finding and
the causal `blockers` that forced a full-suite decision, every waiver, the
per-test verdicts (`selected` with reasons, `skipped` with
witness ids, `always_run` with the capturing finding), and stats, including
`estimated_seconds_saved` when `--durations` was given.

### `acquit/selection-v2` (the selection)

The document the pytest plugin obeys. It lists the provably skippable files
and binds the analyzed tree: `mode`, `graph_hash`, `tree.head_sha`,
`tree.fingerprint`, `artifacts` (repo-relative paths of the three documents,
null when they landed outside the repo), and `skip` entries of
`{path, witness}`. Anything not listed runs; a tree that no longer matches
the fingerprint runs everything. Its optional `canary` block mirrors the
static selected/always-run reasons, changed-path classification, and causal
fallback findings for the observation-only plugin; it is never used to
enforce a skip. See
[ADR 0007](adr/0007-selection-v2-tree-binding.md) for why.

### `acquit/witnesses-v1` (the witnesses)

The evidence behind every skip: the graph hash, a `closures` table mapping
each closure hash to its full sorted file listing, and one witness per
skipped test with `id`, `test`, `closure` (the sha256 of the sorted closure
listing), `changed` (the full changed set), and the `claim`
(`closure(test) does not intersect changed set`). `acquit replay` re-verifies
all of it.

## Environment variables

| Variable | Read by | Meaning |
| --- | --- | --- |
| `ACQUIT_SELECTION_FILE` | the pytest plugin | Path to a selection document. Unset or empty, the plugin is inert and pytest is untouched. Set, the plugin verifies the document once per session (schema, mode, graph hash, tree fingerprint) and deselects the listed files; any failure means every test runs, stated in the pytest header. Explicitly named files on the pytest command line always run. |
| `ACQUIT_CANARY` | the pytest plugin | Set to a literal `1` alongside `ACQUIT_SELECTION_FILE`, switches the plugin to canary mode: the document is verified exactly as usual but nothing is deselected. Every test runs, and at session end the plugin classifies outcomes against the would-be-skipped files, printing a loud `acquit canary: ALARM:` line (with the witness id) for any that failed, or a `clean` line when all passed. It writes `acquit/canary-v2` JSON and a Markdown summary beside the selection file (with `.canary.json` and `.canary.md` suffixes). The exit status is never altered. Any other value leaves ordinary enforce behavior untouched. |
| `ACQUIT_CACHE_DIR` | `select`, `analyze`, `explain` | Base directory for the parse cache. Unset, the cache lives in the platform user cache directory (never inside the checkout), namespaced per repository root. Replay never uses a cache. |
| `GITHUB_TOKEN` | `comment` | Token for the PR comment API calls. Required for `comment`; without it the comment is skipped with a warning and CI is unaffected. |
| `GITHUB_REPOSITORY`, `GITHUB_REF`, `GITHUB_API_URL` | `comment` | The `owner/repo` slug (required), the PR ref used to infer the number when `--pr` is not given, and an optional API endpoint override. |
| `GITHUB_OUTPUT`, `GITHUB_STEP_SUMMARY` | `ci-outputs` | The Actions runner files the outputs and summary are appended to. |

## Canary evidence

Canary is observation-only: it never changes pytest's exit code or what the
plugin deselects. A verified canary run writes two companion files beside its
selection document:

- `*.canary.json` is the versioned `acquit/canary-v2` artifact. It records
  static selection context for each pytest node, repo-relative modules first
  loaded during that node's fixture/setup/call/teardown protocol, fixture
  providers, observed plugin modules, explicit uncertainty markers, and the
  shadow-validation result. A failing would-be-skipped test is a
  high-severity `missed-impact` event.
- `*.canary.md` is the short reviewer summary of the same evidence.

The bundled self-host workflow uploads both files as `acquit-canary-evidence`.
Other workflows can upload the paths returned by `canary-report-file` and
`canary-summary-file`. Retention is controlled by that workflow's artifact
policy; Acquit does not persist evidence or environment snapshots itself.
The stable `stats`, `runtime_edges`, and `missed_impact` fields can be
aggregated across retained artifacts to measure fallback and missed-impact
rates.

The artifact intentionally includes only repo-relative paths, fixture names,
and plugin module names, never absolute paths, environment values, or command
lines. A refused/stale selection produces an `incomplete` record with its
reason rather than a validation claim. Runtime evidence is not an enforcement
input in this release; any future consumer must treat incomplete, stale, or
unknown evidence as a full-suite fallback.

## The GitHub Action

The action is a composite wrapper over this CLI
(see [ADR 0004](adr/0004-composite-action-pinning-pypi.md)); its inputs and
outputs mirror [action.yml](../action.yml).

### Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `base-ref` | the PR base | Base ref to diff against. |
| `acquit-version` | `0.1.3` <!-- x-release-please-version --> | Version of the acquit package to run. |
| `acquit-source` | `pypi` | Where acquit itself is installed from. `pypi` pins `acquit==acquit-version`; `local` installs from the working-directory checkout, which is how acquit dogfoods unreleased code on its own PRs. |
| `working-directory` | `.` | Directory containing the project to analyze. |
| `mode` | `report` | `report`: analyze, verify, comment, and set outputs, but never skip a test. `canary`: additionally export `ACQUIT_SELECTION_FILE` and `ACQUIT_CANARY` so the full suite still runs while the pytest plugin classifies outcomes against the verified selection; a failing would-be-skipped test raises a loud alarm at zero risk. `enforce`: export `ACQUIT_SELECTION_FILE` alone so the pytest plugin honors the verified selection and skips. Anything unrecognized means `report`. |
| `comment` | `true` | Post a sticky PR comment with the decision (`true`/`false`). |

### Outputs

| Output | Meaning |
| --- | --- |
| `mode` | Selection mode after verification: `run-all` or `selective`. |
| `report-file` | Path to the report JSON. |
| `selection-file` | Path to the selection JSON consumed by the pytest plugin. |
| `canary-report-file` | Path where canary JSON evidence is written after pytest finishes. |
| `canary-summary-file` | Path where the concise Markdown canary summary is written after pytest finishes. |
| `skipped-count` | Number of test files provably unaffected. |
| `selected-count` | Number of test files selected by impact. |
| `always-run-count` | Number of test files forced to run by a policy finding. |
| `total-count` | Total number of test files considered. |

The action replays the evidence before a selective run is honored: a failed
replay rewrites the selection to run-all, and the `mode` output is read back
from the document pytest obeys, never from a separate probe.
