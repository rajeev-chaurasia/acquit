"""Behaviors probed adversarially and found sound, locked in as regressions.

These passed the same attack method as the xfail reproductions: a scenario
built to produce an unsafe skip failed to produce one because a rule or the
graph already covers it.
"""

from acquit.pipeline import run_select
from acquit.report import SelectionMode
from adversarial.conftest import AdvRepo, buckets, run_git


def test_acquit_toml_change_forces_run_all(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            ".acquit.toml": "assume_inert = []\n",
            "alpha.py": "A = 1\n",
            "tests/test_alpha.py": "import alpha\n\n\ndef test_a():\n    assert alpha.A\n",
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({".acquit.toml": 'assume_inert = ["docs/*"]\n'})
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    assert result.decision.mode is SelectionMode.RUN_ALL
    assert result.decision.skipped == ()


def test_pyproject_pytest_section_change_forces_run_all(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            "alpha.py": "A = 1\n",
            "tests/test_alpha.py": "import alpha\n\n\ndef test_a():\n    assert alpha.A\n",
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write(
        {
            "pyproject.toml": (
                "[tool.pytest.ini_options]\n"
                'testpaths = ["tests"]\n'
                'python_files = ["test_*.py", "check_*.py"]\n'
            ),
        }
    )
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    assert result.decision.mode is SelectionMode.RUN_ALL
    assert result.decision.skipped == ()


def test_root_conftest_outside_testpaths_forces_run_all(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            "conftest.py": "X = 1\n",
            "tests/test_a.py": "def test_a():\n    assert True\n",
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"conftest.py": "X = 2\n"})
    head = adv_repo.commit("head")

    result = run_select(base, head, adv_repo.path)
    assert result.decision.mode is SelectionMode.RUN_ALL
    assert result.decision.skipped == ()


def test_namespace_shadowing_selects_consumer_for_either_candidate(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "src/pkg/mod.py": "SRC = 1\n",
            "pkg/mod.py": "ROOT = 1\n",
            "tests/test_pkg.py": "from pkg import mod\n\n\ndef test_m():\n    assert mod\n",
            "tests/test_other.py": "def test_o():\n    assert True\n",
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"src/pkg/mod.py": "SRC = 2\n"})
    head = adv_repo.commit("src copy changed")
    adv_repo.write({"pkg/mod.py": "ROOT = 2\n"})
    head2 = adv_repo.commit("root copy changed")

    for base_ref, head_ref in ((base, head), (head, head2)):
        selected, skipped, always = buckets(run_select(base_ref, head_ref, adv_repo.path))
        assert "tests/test_pkg.py" in selected | always
        assert "tests/test_other.py" in skipped


def test_module_and_package_collision_selects_consumer(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "pkg.py": "WHICH = 'module'\n",
            "pkg/__init__.py": "WHICH = 'package'\n",
            "tests/test_pkg.py": "import pkg\n\n\ndef test_p():\n    assert pkg.WHICH\n",
            "tests/test_other.py": "def test_o():\n    assert True\n",
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg.py": "WHICH = 'module2'\n"})
    head = adv_repo.commit("module changed")
    adv_repo.write({"pkg/__init__.py": "WHICH = 'package2'\n"})
    head2 = adv_repo.commit("package changed")

    for base_ref, head_ref in ((base, head), (head, head2)):
        selected, skipped, always = buckets(run_select(base_ref, head_ref, adv_repo.path))
        assert "tests/test_pkg.py" in selected | always
        assert "tests/test_other.py" in skipped


def test_reexport_chain_selects_consumer(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "pkg/__init__.py": "from pkg.middle import thing\n",
            "pkg/middle.py": "from pkg.inner import thing\n",
            "pkg/inner.py": "def thing():\n    return 1\n",
            "tests/test_thing.py": (
                "from pkg import thing\n\n\ndef test_t():\n    assert thing() == 1\n"
            ),
            "tests/test_other.py": "def test_o():\n    assert True\n",
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"pkg/inner.py": "def thing():\n    return 2\n"})
    head = adv_repo.commit("head")

    selected, skipped, always = buckets(run_select(base, head, adv_repo.path))
    assert "tests/test_thing.py" in selected | always
    assert "tests/test_other.py" in skipped


def test_staged_and_unstaged_changes_are_both_seen(adv_repo: AdvRepo) -> None:
    adv_repo.write(
        {
            "alpha.py": "A = 1\n",
            "beta.py": "B = 1\n",
            "tests/test_alpha.py": "import alpha\n\n\ndef test_a():\n    assert alpha.A\n",
            "tests/test_beta.py": "import beta\n\n\ndef test_b():\n    assert beta.B\n",
        }
    )
    base = adv_repo.commit("base")
    adv_repo.write({"alpha.py": "A = 2\n"})
    run_git(adv_repo.path, "add", "alpha.py")
    adv_repo.write({"beta.py": "B = 2\n"})

    result = run_select(base, None, adv_repo.path)
    assert {change.path for change in result.changed} == {"alpha.py", "beta.py"}
    selected, skipped, _ = buckets(result)
    assert {"tests/test_alpha.py", "tests/test_beta.py"} <= selected
    assert skipped == set()
