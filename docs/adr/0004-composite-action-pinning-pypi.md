# ADR 0004: a composite action that pins the PyPI package

Status: accepted

## Context

The GitHub Action and the CLI must be the same tool: the action's value is
that its selection was produced, verified, and replayed by exactly the code a
user could run locally. GitHub offers three ways to build an action: a Docker
container action, a JavaScript action, and a composite action made of plain
steps. Whatever the wrapper is, the fail-closed guarantees (a broken tool
writes a run-all selection, replay gates every selective run) live in the
Python package and must not be re-implemented anywhere else.

## Decision

The action is a composite that installs the published package and calls it:
`uvx --from acquit==<acquit-version> acquit ...` for select, replay,
ci-outputs, and comment. One version string covers both halves: the
`acquit-version` input defaults to the version the action shipped with, so
pinning the action pins the analysis, and a release bumps the package and the
default together.

Rejected:

- Docker action. A container build or pull on every job is exactly the cold
  start a test-selection tool exists to avoid, and Docker container actions
  only run on Linux runners, which would strand the macOS and Windows
  projects the package itself supports.
- JavaScript action. Any logic in the wrapper (deciding modes, parsing
  documents, gating on replay) would duplicate soundness-critical behavior
  in a second language with its own bugs. The composite keeps the shell
  layer to plumbing: write run-all documents on any failure, read the mode
  back from the document pytest will actually obey, and let the package do
  everything that matters.

The `acquit-source: local` input is the escape hatch for code that is not on
PyPI yet: it installs from the working-directory checkout instead of the
pinned release, which is how acquit dogfoods unreleased code on its own pull
requests. Self-hosters can use it the same way, accepting that they are
trusting the checkout rather than a pinned artifact.

## Consequences

- The action stays a thin, auditable shell script; every guarantee worth
  reviewing is in the package, once.
- `uvx` resolves and caches the pinned package per runner, so warm runs cost
  almost nothing and cold runs cost one small wheel install.
- The bash steps are themselves attack surface (runner file injection,
  document sniffing), so they are covered by the adversarial delivery-layer
  suite rather than assumed safe.
