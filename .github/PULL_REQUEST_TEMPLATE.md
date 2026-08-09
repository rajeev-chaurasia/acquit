<!--
The PR title becomes the squash commit on main, so it must be a valid
conventional commit line (feat:, fix:, docs:, test:, chore:, ci:).
-->

## What changed

<!-- One or two sentences. Link the issue if there is one. -->

## Soundness impact

<!--
Soundness-critical code is anything a wrong answer can turn into an unsafe
skip: policy rules and engine, graph assembly and resolution, selection,
witnesses, replay, the tree fingerprint, the pytest plugin, the action's
shell steps. CONTRIBUTING.md spells out the bar.
-->

- [ ] No: this change cannot affect which tests are skipped.
- [ ] Yes: it can. The fixture or adversarial test that proves the change
      safe is: <!-- e.g. tests/adversarial/test_..., or the rule + fixture repo -->

## Gates

- [ ] `uv run ruff check .` and `uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] `uv run python scripts/check_style.py`
- [ ] `uv run pytest --cov`
