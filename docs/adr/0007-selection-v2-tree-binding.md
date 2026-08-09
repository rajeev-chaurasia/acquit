# ADR 0007: selection documents bind the analyzed tree

Status: accepted

## Context

The selection document is the last hop between a proof and a test run: it is
the file the pytest plugin obeys. The original selection-v1 document listed
skippable paths and a graph hash, and the adversarial pass against the
delivery layer showed that was not enough. A stale document (a retried job, a
cached workspace, a rebase) can describe a tree that has moved on, and the
graph hash cannot detect it: two different diffs over the same tree produce
the same graph and therefore the same hash, while their correct skip sets are
disjoint. A document copied from a different project deselects files by
relative path in a repository it never analyzed. Both were demonstrated as
reproductions in the adversarial suite before the fix, and both would have
been honored.

## Decision

A selective selection-v2 document binds the exact tree it analyzed, and the
plugin refuses to apply it anywhere else.

- Binding: the document carries the graph hash, the head sha when one exists,
  and a content fingerprint of the analyzed tree. The fingerprint hashes the
  blob shas of every tracked and untracked file (clean tracked files reuse
  their index shas; dirty and untracked files are hashed from disk), so it
  identifies the working tree's content, not just its commit.
- Verification: before any deselection, once per session, the plugin resolves
  the enclosing repository root, recomputes the fingerprint with the same
  code select used, and requires schema, mode, graph hash, and fingerprint to
  line up. Any mismatch, and any surprise at all, means every test runs and
  the header says why.
- The artifacts self-exemption: select's own three output documents would
  otherwise poison the fingerprint the moment they are written into the
  checkout. The selection document records where they landed (repo-relative,
  null when outside the repo), and both fingerprint computations exclude
  those paths plus the selection file itself. This is sound: excluding
  acquit's freshly written documents cannot hide a user change, and once a
  user commits them they are tracked diff content covered by R001 like any
  other resource.
- Replay is the trust anchor. Tree binding proves the document describes the
  tree in front of pytest; it does not prove the document tells the truth.
  `acquit replay` rebuilds the snapshot at the recorded head sha with no
  cache, re-verifies every witness from first principles, and cross-checks
  the selection document's skip list against the report. The action runs
  replay before a selective run is honored and rewrites the document to
  run-all when it fails, which is also the backstop against poisoned CI
  caches and forged documents.

## Consequences

- A selection document is useless outside the exact tree it was built from,
  which is the point: honoring it anywhere else was demonstrated unsafe.
- Working-tree selections work locally without a commit, at the cost of not
  being replayable; runs that need the replay gate select with `--head` on a
  real commit.
- The plugin never trusts its input: schema, size, entry shapes, and the
  fingerprint are all checked, and every refusal degrades to running
  everything, stated on the pytest header rather than raised as a warning.
