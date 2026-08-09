# Show HN post

Title:

Show HN: Acquit, provable test skipping for pytest (replayed 198 PRs, 0 unsafe skips)

Text:

I built acquit to test a bet: static import analysis in Python can be sound if you fail closed and prove every skip. It parses imports, pytest config, and conftest scoping (never executes your code), works out which test files a PR cannot possibly affect, and attaches a machine-checkable witness to every skip: the test's import closure hash, the changed set, and a disjointness claim. `acquit replay` rebuilds the graph from scratch and re-verifies every witness before pytest is allowed to honor a selective run. Everything the analysis cannot bound (dynamic imports, sys.path games, changed conftests, dependency bumps, non-Python files) goes through an enumerated rule table whose every entry converges on "run everything".

To check the soundness claim against reality, I replayed 198 merged PRs from click, flask, rich, and httpx: full suite at base and head in a frozen environment, diff the outcomes, and verify no test whose outcome changed (or that only exists at head) lived in a skipped file. Result: 0 unsafe skips, and when acquit is selective it skips 93 to 99 percent of suite time. The catch, covered honestly in the writeup, is that only 5 percent of PRs are selective out of the box, mostly because changelog and docs edits ride along with everything (one config entry lifts that to 60 percent), and the study caught one of my own fixes overshooting into uselessness. Full study with methodology and warts: https://github.com/rajeev-chaurasia/acquit/blob/main/docs/study.md
