"""Confirmed unsafe skips: the decision skips a test whose outcome changed.

Each reproduction builds a repository where the victim test passes at base and
fails at head, asserts that ground truth with a real pytest run against the
head working tree, then asserts the victim was not skipped. The strict xfails
document open soundness holes: a fix turns them into xpasses, flagging the
marker for removal.
"""

import pytest

from acquit.pipeline import run_select
from adversarial.conftest import AdvRepo, buckets


@pytest.mark.xfail(
    strict=True,
    reason="ADV-1: pytest inserts the test file's directory into sys.path, the index does not",
)
def test_import_from_test_directory_without_init(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            "tests/helper.py": "VALUE = 1\n",
            "tests/test_a.py": ("import helper\n\n\ndef test_a():\n    assert helper.VALUE == 1\n"),
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"tests/helper.py": "VALUE = 2\n"})
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "tests/test_a.py" not in skipped


@pytest.mark.xfail(
    strict=True,
    reason="ADV-2: the pythonpath ini option is parsed but never becomes an import root",
)
def test_import_through_pythonpath_ini(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "pytest.ini": "[pytest]\npythonpath = lib\ntestpaths = tests\n",
            "lib/helper.py": "VALUE = 1\n",
            "tests/test_helper.py": (
                "import helper\n\n\ndef test_value():\n    assert helper.VALUE == 1\n"
            ),
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"lib/helper.py": "VALUE = 2\n"})
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "tests/test_helper.py" not in skipped


@pytest.mark.xfail(
    strict=True,
    reason="ADV-3: working-tree diffs miss untracked files that the head snapshot includes",
)
def test_untracked_conftest_invisible_to_working_tree_diff(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "alpha.py": "ALPHA = 1\n",
            "tests/test_alpha.py": (
                "import alpha\n\n\ndef test_alpha():\n    assert alpha.ALPHA == 1\n"
            ),
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write(
        {
            "conftest.py": (
                "import pytest\n\n\n"
                "@pytest.fixture(autouse=True)\n"
                "def boom():\n"
                "    raise RuntimeError('new conftest changes every test')\n"
            ),
        }
    )

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, None, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "tests/test_alpha.py" not in skipped


@pytest.mark.xfail(
    strict=True,
    reason="ADV-4: pytest_plugins declared in a test module never becomes a plugin edge",
)
def test_pytest_plugins_declared_in_test_module(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "plugmod.py": ("import pytest\n\n\n@pytest.fixture\ndef widget():\n    return 1\n"),
            "test_plug.py": (
                "pytest_plugins = ['plugmod']\n\n\n"
                "def test_widget(widget):\n"
                "    assert widget == 1\n"
            ),
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write(
        {
            "plugmod.py": ("import pytest\n\n\n@pytest.fixture\ndef widget():\n    return 2\n"),
        }
    )
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_plug.py" not in skipped


@pytest.mark.xfail(
    strict=True,
    reason="ADV-5: a literal relative dynamic import resolves to external '' with no taint",
)
def test_relative_dynamic_import_literal(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "pkg/__init__.py": "",
            "pkg/helper.py": "VALUE = 1\n",
            "test_dyn.py": (
                "import importlib\n\n\n"
                "def test_dyn():\n"
                "    mod = importlib.import_module('.helper', 'pkg')\n"
                "    assert mod.VALUE == 1\n"
            ),
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/helper.py": "VALUE = 2\n"})
    head = adv_repo.commit("head")

    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "test_dyn.py" not in skipped


@pytest.mark.xfail(
    strict=True,
    reason="ADV-6: a sys.path mutation taints only its importers but leaks process-wide",
)
def test_sys_path_mutation_leaks_across_tests(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "conftest.py": "",
            "pathmod.py": (
                "import sys\n"
                "from pathlib import Path\n\n"
                "sys.path.insert(0, str(Path(__file__).parent / 'lib'))\n"
            ),
            "lib/weird.py": "VALUE = 1\n",
            "tests/test_a.py": (
                "import pathmod  # noqa: F401\n"
                "import weird\n\n\n"
                "def test_a():\n"
                "    assert weird.VALUE >= 1\n"
            ),
            "tests/test_b.py": ("import weird\n\n\ndef test_b():\n    assert weird.VALUE == 1\n"),
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"lib/weird.py": "VALUE = 2\n"})
    head = adv_repo.commit("head")

    # test_a is collected first, imports pathmod, and the mutated sys.path
    # then serves test_b's import of weird in the same process.
    assert adv_repo.run_pytest().returncode != 0

    result = run_select(base, head, adv_repo.path)
    _, skipped, _ = buckets(result)
    assert "tests/test_b.py" not in skipped
