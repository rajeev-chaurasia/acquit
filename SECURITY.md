# Security policy

Acquit decides which tests do not run. That makes its failure modes security
issues in a way most dev tools' are not: a bug that produces an unsafe skip
can wave a broken or malicious change through CI while reporting proof that
it could not matter.

## Supported versions

The latest minor release line receives security fixes.

| Version | Supported |
| --- | --- |
| latest minor (currently 0.0.x) | yes |
| anything older | no |

## Reporting

Use GitHub private vulnerability reporting on this repository: the Security
tab, then "Report a vulnerability". Please do not open a public issue for
anything you believe is exploitable before it is fixed.

For an unsafe skip you can reproduce and are comfortable sharing publicly,
the unsafe-skip issue template is fine too; the report and witnesses
documents it asks for are what make a reproduction actionable.

## What counts as a security issue here

- An unsafe skip: any construction (diff, repository layout, configuration,
  cache state) where acquit skips a test whose outcome the change actually
  affects. This is the defining failure and is treated as a release blocker:
  a confirmed unsafe skip blocks the next release until fixed, and the fix
  ships with a committed adversarial reproduction.
- Cache poisoning: any way a crafted parse-cache entry survives into a
  selective decision that replay then accepts.
- Selection document forgery: any way a document the analysis did not
  produce for this exact tree gets honored by the pytest plugin, or a forged
  witness passes `acquit replay`.
- The usual classes too: code execution during analysis (the analysis must
  never execute the code it reads), or injection through the action's
  runner files (outputs, env, step summaries).

Precision problems (acquit running tests it did not need to) are ordinary
bugs, not security issues; the tool is designed to fail in that direction.

## Trust boundaries

The boundaries below are documented, deliberate, and worth reading before
reporting; the full statements live in [docs/soundness.md](docs/soundness.md).

- The head commit vouches for itself through acquit's own configuration:
  `assume_inert` globs and waivers are read at head, so a PR can excuse its
  own change in the same diff. Reviewers should treat `.acquit.toml` diffs
  as security-relevant, like CI workflow changes.
- CI cache restore keys remain a trust boundary at selection time. Replay is
  the backstop: it rebuilds without any cache and refuses forged witnesses,
  and the shipped action replays the evidence before a selective run is
  honored. A poisoned cache that only affects selection, and is caught by
  replay, is working as designed; one that survives replay is a
  vulnerability.
- Test independence (assumption A4) is the one assumption acquit cannot
  check: suites whose tests depend on each other's side effects are outside
  the contract.

## What to include

The [unsafe-skip issue template](.github/ISSUE_TEMPLATE/unsafe-skip.md)
doubles as the checklist: a repo and sha pair or a minimal reproduction, the
report and witnesses documents, and which test outcome changed. For private
reports, the same material through the private channel.
