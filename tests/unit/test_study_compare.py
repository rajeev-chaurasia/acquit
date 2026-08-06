"""The safety check: planted unsafe skips must be caught."""

from acquit.study.compare import changed_tests, check_safety, parse_quarantine
from acquit.study.outcomes import Outcome

PASSED = frozenset({Outcome.PASSED})
FAILED = frozenset({Outcome.FAILED})
NONE = frozenset[str]()


def test_planted_unsafe_skip_is_caught() -> None:
    base = {"tests/test_a.py::test_x": PASSED, "tests/test_b.py::test_y": PASSED}
    head = {"tests/test_a.py::test_x": FAILED, "tests/test_b.py::test_y": PASSED}
    result = check_safety(base, head, ["tests/test_a.py"], NONE)
    assert result.unsafe_skips == ("tests/test_a.py",)
    assert result.changed_outcomes == ("tests/test_a.py::test_x",)
    assert not result.safe


def test_clean_selection_is_safe() -> None:
    base = {"tests/test_a.py::test_x": PASSED, "tests/test_b.py::test_y": PASSED}
    head = {"tests/test_a.py::test_x": FAILED, "tests/test_b.py::test_y": PASSED}
    result = check_safety(base, head, ["tests/test_b.py"], NONE)
    assert result.unsafe_skips == ()
    assert result.new_tests_selected
    assert result.safe


def test_skipped_new_test_is_caught() -> None:
    base = {"tests/test_a.py::test_x": PASSED}
    head = {"tests/test_a.py::test_x": PASSED, "tests/test_new.py::test_n": PASSED}
    result = check_safety(base, head, ["tests/test_new.py"], NONE)
    assert not result.new_tests_selected
    # A head-only test also counts as a changed outcome in a skipped file.
    assert result.unsafe_skips == ("tests/test_new.py",)
    assert not result.safe


def test_quarantine_filters_changed_outcomes() -> None:
    base = {"tests/test_a.py::test_x": PASSED}
    head = {"tests/test_a.py::test_x": FAILED}
    quarantine = frozenset({"tests/test_a.py::test_x"})
    result = check_safety(base, head, ["tests/test_a.py"], quarantine)
    assert result.changed_outcomes == ()
    assert result.unsafe_skips == ()
    assert result.safe


def test_quarantine_never_excuses_a_skipped_new_test() -> None:
    base: dict[str, frozenset[Outcome]] = {}
    head = {"tests/test_new.py::test_n": PASSED}
    quarantine = frozenset({"tests/test_new.py::test_n"})
    result = check_safety(base, head, ["tests/test_new.py"], quarantine)
    assert not result.new_tests_selected
    assert not result.safe


def test_outcome_set_growth_counts_as_change() -> None:
    base = {"tests/test_a.py::test_x": PASSED}
    head = {"tests/test_a.py::test_x": frozenset({Outcome.PASSED, Outcome.FAILED})}
    assert changed_tests(base, head, NONE) == frozenset({"tests/test_a.py::test_x"})


def test_removed_test_is_not_a_change() -> None:
    base = {"tests/test_a.py::test_x": PASSED, "tests/test_a.py::test_gone": PASSED}
    head = {"tests/test_a.py::test_x": PASSED}
    assert changed_tests(base, head, NONE) == frozenset()


def test_parse_quarantine_skips_comments_and_blanks() -> None:
    text = "# flaky since 2026\n\ntests/test_a.py::test_x\n  tests/test_b.py::test_y  \n#x\n"
    assert parse_quarantine(text) == frozenset(
        {"tests/test_a.py::test_x", "tests/test_b.py::test_y"}
    )
