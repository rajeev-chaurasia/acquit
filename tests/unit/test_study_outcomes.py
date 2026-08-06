"""Junit parsing and node-id normalization for the study."""

import pytest

from acquit.errors import AcquitError
from acquit.study.outcomes import Outcome, file_of, normalize_node_id, parse_junit

XUNIT2 = """\
<testsuites>
  <testsuite name="pytest" tests="7">
    <testcase classname="tests.test_math.TestOps" name="test_add[1-2]" time="0.01"/>
    <testcase classname="tests.test_math.TestOps" name="test_add[3-4]" time="0.02"/>
    <testcase classname="tests.test_math" name="test_top" time="0.10">
      <failure message="boom"/>
    </testcase>
    <testcase classname="tests.test_io" name="test_err" time="0.05">
      <error message="err"/>
    </testcase>
    <testcase classname="tests.test_io" name="test_skip" time="0.00">
      <skipped message="later"/>
    </testcase>
    <testcase classname="tests.test_mixed" name="test_flaky[a]" time="0.01"/>
    <testcase classname="tests.test_mixed" name="test_flaky[b]" time="0.01">
      <failure message="only under b"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def test_parametrize_collapses_and_class_is_kept() -> None:
    suite = parse_junit(XUNIT2)
    assert suite.by_test["tests/test_math.py::TestOps::test_add"] == frozenset({Outcome.PASSED})
    assert "tests/test_math.py::TestOps::test_add[1-2]" not in suite.by_test


def test_error_and_failed_are_distinct() -> None:
    suite = parse_junit(XUNIT2)
    assert suite.by_test["tests/test_math.py::test_top"] == frozenset({Outcome.FAILED})
    assert suite.by_test["tests/test_io.py::test_err"] == frozenset({Outcome.ERROR})
    assert suite.by_test["tests/test_io.py::test_skip"] == frozenset({Outcome.SKIPPED})


def test_mixed_parametrized_outcomes_form_a_set() -> None:
    suite = parse_junit(XUNIT2)
    assert suite.by_test["tests/test_mixed.py::test_flaky"] == frozenset(
        {Outcome.PASSED, Outcome.FAILED}
    )


def test_file_durations_sum_per_file() -> None:
    suite = parse_junit(XUNIT2)
    assert suite.file_durations["tests/test_math.py"] == pytest.approx(0.13)
    assert suite.file_durations["tests/test_io.py"] == pytest.approx(0.05)


def test_xunit1_file_attribute_wins() -> None:
    xml = """\
<testsuite tests="1">
  <testcase classname="tests.sub.test_x.TestA.TestInner" name="test_y[p]"
            file="tests/sub/test_x.py" line="7" time="0.01"/>
</testsuite>
"""
    suite = parse_junit(xml)
    assert set(suite.by_test) == {"tests/sub/test_x.py::TestA::TestInner::test_y"}


def test_normalize_keeps_nested_classes() -> None:
    node = normalize_node_id("tests.test_a.TestOuter.TestInner", "test_z[1]")
    assert node == "tests/test_a.py::TestOuter::TestInner::test_z"
    assert file_of(node) == "tests/test_a.py"


def test_normalize_module_level_function() -> None:
    assert normalize_node_id("tests.test_a", "test_b") == "tests/test_a.py::test_b"


def test_normalize_backslash_file_attribute() -> None:
    node = normalize_node_id("tests.test_a", "test_b", file="tests\\test_a.py")
    assert node == "tests/test_a.py::test_b"


def test_nameless_testcases_are_ignored() -> None:
    xml = '<testsuite><testcase classname="tests.test_a" time="1.0"/></testsuite>'
    assert parse_junit(xml).by_test == {}


def test_malformed_xml_raises_acquit_error() -> None:
    with pytest.raises(AcquitError):
        parse_junit("<testsuite><unclosed")
