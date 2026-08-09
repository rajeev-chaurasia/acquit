# ADR 0003: rustworkx as the graph engine

Status: accepted

## Context

Every decision acquit makes is a graph query. Selection is reverse
reachability from the changed files to the test nodes; every skip needs the
test's full import closure, hashed into a witness; replay recomputes every one
of those closures from a fresh graph before a selective run is honored. On a
real repository that is thousands of nodes and tens of thousands of edges,
traversed several times per run, and the analysis budget is "under a second
per PR". The package also has a hard dependency budget: the action installs
acquit into CI with `uvx`, next to whatever the project under test needs, so
every runtime dependency is a cost and a potential conflict.

The candidates were networkx, a hand-rolled adjacency structure, and
rustworkx.

## Decision

rustworkx is the graph engine and the only runtime dependency.

- Traversal happens in native code. `rx.descendants` gives a test's import
  closure and `rx.ancestors` gives reverse reachability (multi-source queries
  add a temporary sink node and ask for its ancestors), so the hot path never
  loops over edges in Python. networkx does the same queries in pure Python
  and was an order of magnitude slower on the closure-per-skip workload that
  replay repeats for every witness.
- Nodes and edges carry typed payloads: `PyDiGraph[Node, EdgeKind]` holds the
  frozen `Node` dataclass and the `EdgeKind` enum directly, rustworkx ships
  type stubs, and the annotations survive mypy strict. A hand-rolled
  structure could match this, but it would mean maintaining and testing a
  graph library inside a tool whose correctness argument is already large.
- Determinism stays in acquit's hands. Node identity is the repo-relative
  path, insertion and edge order are fixed by the assembler, and the graph
  hash covers a canonical (nodes, edges, schema version) form, so the engine
  underneath can reorder its internals without changing a single document.

Where determinism needs an ordered traversal that rustworkx does not promise,
acquit still hand-rolls it: explain's dependency paths come from its own BFS
with sorted tie-breaks, so the same question always prints the same chain.

## Consequences

- One runtime dependency total, with prebuilt wheels for every OS and Python
  version in the CI matrix; `uvx --from acquit==X` stays fast and light.
- Closure recomputation is cheap enough that replay can re-verify every
  witness on every selective CI run instead of sampling.
- A Rust extension is opaque to step-through debugging, which is tolerable
  because everything around the queries (assembly, hashing, decisions) is
  plain Python and the queries themselves are standard reachability.
