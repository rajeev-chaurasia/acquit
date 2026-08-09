---
name: Bug report
about: Anything else that misbehaves. A wrong skip has its own template; use it,
  it gets priority.
title: ""
labels: bug
---

<!--
For a test that was skipped and should not have been, use the unsafe-skip
template instead; those reports block releases.

Over-selection (acquit running tests it did not need to) is a known cost of
the fail-closed design, but reports are still welcome: the reason histogram
is the roadmap. Include the findings table from the report or the PR comment.
-->

## What happened

A clear description, plus the exact command and its output. The one-line
summary acquit printed (`acquit: selective: ...` or `acquit: run-all: ...`)
and, when relevant, the `acquit-report.json`.

## What you expected

## Environment

- Acquit version (`acquit --version`):
- Python version:
- OS:
- How acquit ran (CLI, pytest plugin, GitHub Action with `mode:` value):

## Reproduction

The smallest repo layout, diff, and command that shows the problem. For
decision questions, the output of
`acquit explain <test-file> --base <ref>` is usually the fastest diagnostic.
