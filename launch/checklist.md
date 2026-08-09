# Launch checklist

The remaining steps between here and the public posts, in order.

## 1. Ship 0.1.0

- [ ] Approve CI on the open release-please PR (bot PRs need the one-click workflow approval), confirm it goes green, merge it.
- [ ] Verify the release workflow publishes 0.1.0 to PyPI via trusted publishing and creates the GitHub release.
- [ ] When publishing the GitHub release, tick the "Publish this Action to the GitHub Marketplace" checkbox on the release form. Check the listing renders correctly (name, icon, README excerpt).

## 2. Post-release doc sweep

- [ ] Update the README quickstart from `uses: rajeev-chaurasia/acquit@main` to the pinned `@v0.1.0` (and the closing sentence about pinning in "Try it on a PR").
- [ ] Update the README status line: Action is on the Marketplace, CLI pin becomes `acquit==0.1.0`.
- [ ] Same pin fix in docs/study.md ("Try it in report mode" snippet).
- [ ] Fresh-eyes install gate: on a machine or container with no cached state, follow only the README quickstart and confirm the comment appears on a test PR.

## 3. Pre-launch maintainer outreach

Before posting anywhere public, email or open a discussion with the maintainers of the four studied repos (pallets/click, pallets/flask, Textualize/rich, encode/httpx). Short note: I replayed N of your merged PRs as part of a test-selection soundness study, here is the per-repo summary, and here is what acquit flags on your history. Offer a report-only PR (report mode never skips a test, it only posts an explanatory comment) if they want to see it live, and make clear there is no expectation. Two reasons: they should hear about a study of their repos from me and not from an HN thread, and a maintainer reply in the thread is worth more than anything I can write.

- [ ] click maintainers contacted
- [ ] flask maintainers contacted
- [ ] rich maintainers contacted
- [ ] httpx maintainers contacted

## 4. Post order and timing

- [ ] Show HN first. Text is in launch/shownh.md. Post Tuesday to Thursday, morning US Pacific (roughly 7 to 9am PT). Do not ask anyone to upvote; do not repost the same day if it sinks (HN allows a later retry after a cooldown).
- [ ] Stay at the keyboard for the first 3 to 4 hours to answer comments. Expected pushback to prepare for: "static analysis cannot be sound" (answer: fail-closed direction plus the replay evidence), "5 percent selective is useless" (answer: the design working as intended, one config entry, and the honest counterfactual caveat), and testmon comparisons (answer: agree, it is the honest alternative, different tradeoff).
- [ ] r/Python second, one or two days later, whatever the HN outcome. Text is in launch/reddit-rpython.md, flair Showcase; the What/Target/Comparison sections are mandatory there.
- [ ] Only after both: any personal-site or newsletter mirror, linking back to docs/study.md as the canonical writeup.

## 5. After launch

- [ ] Triage issues daily for the first week; an unsafe-skip report is a drop-everything bug.
- [ ] Fold recurring questions back into the README FAQ.
