---
name: Unsafe skip
about: Acquit skipped a test that the change actually affected. The most valuable
  report this project can receive.
title: "unsafe skip: "
labels: unsafe-skip
---

<!--
An unsafe skip means acquit skipped a test file whose outcome the change
actually affected. Confirmed unsafe skips block the next release until fixed,
and the fix ships with a committed adversarial reproduction.

If you believe the issue is exploitable and should not be public yet, use
GitHub private vulnerability reporting instead (Security tab on this repo);
see https://github.com/rajeev-chaurasia/acquit/blob/main/SECURITY.md
-->

## Where it happened

Either a public repo/sha pair (best), or a minimal reproduction:

- Repository:
- Base sha:
- Head sha:
- Acquit version (`acquit --version`):

Or attach/inline a minimal reproduction: the smallest tree plus diff that
produces the skip.

## The evidence documents

Attach both documents from the run that skipped the test:

- `acquit-report.json`
- `acquit-witnesses.json`

If you still have the environment, the output of
`acquit replay <report> --witnesses <witnesses>` as well.

## What outcome changed

- Skipped test file:
- The test(s) inside it whose outcome the change affects:
- Outcome at base (pass/fail/error):
- Outcome at head, when the file is forced to run:
- How you forced it to run (for example, pytest on the file directly):

## Anything else

Configuration in play (`[tool.acquit]` / `.acquit.toml`, `assume_inert`,
waivers), CI caching in use, or anything unusual about the repo layout.
