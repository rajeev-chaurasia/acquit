import random

import pytest

from acquit.config import AcquitConfig, Waiver
from acquit.graph.index import build_index, detect_roots
from acquit.graph.model import NodeKind
from acquit.graph.parse import ModuleFacts, parse_module_facts
from acquit.policy.engine import PolicyContext, PolicyOutcome, evaluate
from acquit.policy.model import Finding, RuleId, Scope, ScopeKind
from acquit.policy.rules import ALL_RULES
from acquit.pytestmap.conftree import UNPARSEABLE_MARKER, ConftestFacts
from acquit.pytestmap.pytestcfg import DEFAULT_NORECURSEDIRS, DEFAULT_PYTHON_FILES, PytestConfig
from acquit.vcs import ChangedFile, ChangeStatus


def make_pytest_config(*, doctest_modules: bool = False, source: str | None = None) -> PytestConfig:
    return PytestConfig(
        source=source,
        python_files=DEFAULT_PYTHON_FILES,
        testpaths=(),
        norecursedirs=DEFAULT_NORECURSEDIRS,
        addopts=(),
        pythonpath=(),
        doctest_modules=doctest_modules,
        extra_plugins=(),
    )


def make_ctx(
    *,
    changed: tuple[ChangedFile, ...] = (),
    kinds: dict[str, NodeKind] | None = None,
    facts: dict[str, ModuleFacts] | None = None,
    conftest_facts: dict[str, ConftestFacts] | None = None,
    unparseable: tuple[str, ...] = (),
    files: tuple[str, ...] = (),
    pytest_config: PytestConfig | None = None,
    config: AcquitConfig | None = None,
) -> PolicyContext:
    return PolicyContext(
        changed=changed,
        kinds=kinds or {},
        facts=facts or {},
        conftest_facts=conftest_facts or {},
        unparseable=unparseable,
        index=build_index(files, detect_roots(files)),
        pytest_config=pytest_config or make_pytest_config(),
        config=config or AcquitConfig(),
    )


def modified(path: str) -> ChangedFile:
    return ChangedFile(path=path, status=ChangeStatus.MODIFIED)


def facts_for(path: str, source: str) -> ModuleFacts:
    return parse_module_facts(source.encode(), path)


def fired_rules(outcome: PolicyOutcome) -> set[RuleId]:
    return {finding.rule for finding in outcome.findings}


def findings_for(outcome: PolicyOutcome, rule: RuleId) -> tuple[Finding, ...]:
    return tuple(finding for finding in outcome.findings if finding.rule is rule)


def test_registry_holds_all_fifteen_engine_rules() -> None:
    assert len(ALL_RULES) == 15


# R001 CHANGED_RESOURCE


@pytest.mark.parametrize(
    ("path", "kind", "fires"),
    [
        ("data/gold.csv", NodeKind.RESOURCE, True),
        ("assets/logo.png", NodeKind.RESOURCE, True),
        ("pkg/mod.py", NodeKind.MODULE, False),
        ("tests/test_x.py", NodeKind.TEST, False),
    ],
)
def test_r001_fires_only_for_resource_kinds(path: str, kind: NodeKind, fires: bool) -> None:
    ctx = make_ctx(changed=(modified(path),), kinds={path: kind})
    assert (RuleId.CHANGED_RESOURCE in fired_rules(evaluate(ctx))) is fires


def test_r001_scope_is_global_and_names_the_file() -> None:
    ctx = make_ctx(changed=(modified("data/gold.csv"),), kinds={"data/gold.csv": NodeKind.RESOURCE})
    (finding,) = findings_for(evaluate(ctx), RuleId.CHANGED_RESOURCE)
    assert finding.scope == Scope(ScopeKind.GLOBAL)
    assert finding.subject == "data/gold.csv"
    assert "data/gold.csv" in finding.reason


@pytest.mark.parametrize(("glob", "fires"), [("docs/*", False), ("*.png", True)])
def test_r001_assume_inert_glob_silences_matching_resources(glob: str, fires: bool) -> None:
    ctx = make_ctx(
        changed=(modified("docs/diagram.svg"),),
        kinds={"docs/diagram.svg": NodeKind.RESOURCE},
        config=AcquitConfig(assume_inert=(glob,)),
    )
    assert (RuleId.CHANGED_RESOURCE in fired_rules(evaluate(ctx))) is fires


def test_r001_deleted_resource_still_fires() -> None:
    deleted = ChangedFile(path="data/fixture.json", status=ChangeStatus.DELETED)
    ctx = make_ctx(changed=(deleted,))
    (finding,) = findings_for(evaluate(ctx), RuleId.CHANGED_RESOURCE)
    assert finding.subject == "data/fixture.json"


# R002 CHANGED_DEPENDENCY_MANIFEST


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("pyproject.toml", True),
        ("backend/setup.py", True),
        ("setup.cfg", True),
        ("Pipfile", True),
        ("Pipfile.lock", True),
        ("uv.lock", True),
        ("poetry.lock", True),
        ("requirements.txt", True),
        ("requirements-dev.txt", True),
        ("deps/constraints-prod.txt", True),
        ("requirements.md", False),
        ("dev-requirements.txt", False),
        ("uv.lock.bak", False),
    ],
)
def test_r002_dependency_manifests(path: str, fires: bool) -> None:
    ctx = make_ctx(changed=(modified(path),), kinds={path: NodeKind.CONFIG})
    assert (RuleId.CHANGED_DEPENDENCY_MANIFEST in fired_rules(evaluate(ctx))) is fires


def test_pyproject_fires_r002_not_r003() -> None:
    ctx = make_ctx(changed=(modified("pyproject.toml"),), kinds={"pyproject.toml": NodeKind.CONFIG})
    rules = fired_rules(evaluate(ctx))
    assert RuleId.CHANGED_DEPENDENCY_MANIFEST in rules
    assert RuleId.CHANGED_TEST_ENVIRONMENT not in rules


# R003 CHANGED_TEST_ENVIRONMENT


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("pytest.ini", True),
        ("ci/tox.ini", True),
        ("vendor/editable.pth", True),
        ("sitecustomize.py", True),
        ("usercustomize.py", True),
        (".github/workflows/ci.yml", True),
        ("Dockerfile", True),
        ("docker/Dockerfile.ci", True),
        ("conftest.py", True),
        (".github/workflows.md", False),
        ("docs/dockerfile-notes.md", False),
        ("pytest.ini.orig", False),
    ],
)
def test_r003_test_environment_files(path: str, fires: bool) -> None:
    ctx = make_ctx(changed=(modified(path),), kinds={path: NodeKind.CONFIG})
    assert (RuleId.CHANGED_TEST_ENVIRONMENT in fired_rules(evaluate(ctx))) is fires


def test_root_conftest_fires_r003_not_r005() -> None:
    ctx = make_ctx(changed=(modified("conftest.py"),), kinds={"conftest.py": NodeKind.CONFTEST})
    rules = fired_rules(evaluate(ctx))
    assert RuleId.CHANGED_TEST_ENVIRONMENT in rules
    assert RuleId.CHANGED_CONFTEST not in rules


# R004 CHANGED_NATIVE_SOURCE


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("src/fast.c", True),
        ("include/api.h", True),
        ("lib/impl.cc", True),
        ("lib/impl.cpp", True),
        ("include/api.hpp", True),
        ("pkg/speed.pyx", True),
        ("pkg/decl.pxd", True),
        ("build/out.so", True),
        ("build/out.pyd", True),
        ("CMakeLists.txt", True),
        ("native/Makefile", True),
        ("meson.build", True),
        ("docs/makefile-notes.md", False),
        ("makefile", False),
        ("pkg/mod.py", False),
    ],
)
def test_r004_native_source(path: str, fires: bool) -> None:
    ctx = make_ctx(changed=(modified(path),), kinds={path: NodeKind.RESOURCE})
    assert (RuleId.CHANGED_NATIVE_SOURCE in fired_rules(evaluate(ctx))) is fires


# R005 CHANGED_CONFTEST


def test_r005_nested_conftest_scopes_to_its_directory() -> None:
    ctx = make_ctx(
        changed=(modified("tests/api/conftest.py"),),
        kinds={"tests/api/conftest.py": NodeKind.CONFTEST},
    )
    (finding,) = findings_for(evaluate(ctx), RuleId.CHANGED_CONFTEST)
    assert finding.scope == Scope(ScopeKind.SUBTREE, "tests/api")
    assert finding.subject == "tests/api/conftest.py"


def test_r005_deleted_conftest_still_invalidates_its_subtree() -> None:
    deleted = ChangedFile(path="tests/api/conftest.py", status=ChangeStatus.DELETED)
    ctx = make_ctx(changed=(deleted,))
    (finding,) = findings_for(evaluate(ctx), RuleId.CHANGED_CONFTEST)
    assert finding.scope == Scope(ScopeKind.SUBTREE, "tests/api")


def test_changed_nested_conftest_double_reports_r005_and_r014() -> None:
    ctx = make_ctx(
        changed=(modified("tests/conftest.py"),),
        kinds={"tests/conftest.py": NodeKind.CONFTEST},
    )
    rules = fired_rules(evaluate(ctx))
    assert {RuleId.CHANGED_CONFTEST, RuleId.CHANGED_TEST_FILE} <= rules


# R006 COLLECTION_ALTERING_HOOK


def test_r006_collection_altering_names_fire_globally() -> None:
    facts = ConftestFacts(
        path="tests/conftest.py",
        collection_altering=("pytest_collect_file",),
        pytest_plugins=(),
    )
    ctx = make_ctx(conftest_facts={"tests/conftest.py": facts})
    (finding,) = findings_for(evaluate(ctx), RuleId.COLLECTION_ALTERING_HOOK)
    assert finding.scope == Scope(ScopeKind.GLOBAL)
    assert "pytest_collect_file" in finding.reason


def test_r006_unparseable_conftest_fires() -> None:
    facts = ConftestFacts(
        path="tests/conftest.py",
        collection_altering=(UNPARSEABLE_MARKER,),
        pytest_plugins=(),
    )
    ctx = make_ctx(conftest_facts={"tests/conftest.py": facts})
    (finding,) = findings_for(evaluate(ctx), RuleId.COLLECTION_ALTERING_HOOK)
    assert "could not be parsed" in finding.reason


def test_r006_benign_conftest_is_silent() -> None:
    facts = ConftestFacts(path="tests/conftest.py", collection_altering=(), pytest_plugins=())
    ctx = make_ctx(conftest_facts={"tests/conftest.py": facts})
    assert RuleId.COLLECTION_ALTERING_HOOK not in fired_rules(evaluate(ctx))


@pytest.mark.parametrize(
    ("entry", "files", "fires"),
    [
        ("celery.contrib.pytest", ("mypkg/__init__.py",), False),
        ("mypkg.plugins", ("mypkg/__init__.py", "mypkg/plugins.py"), False),
        ("mypkg.plugins", ("mypkg/__init__.py",), True),
    ],
)
def test_r006_pytest_plugins_resolution(entry: str, files: tuple[str, ...], fires: bool) -> None:
    facts = ConftestFacts(path="tests/conftest.py", collection_altering=(), pytest_plugins=(entry,))
    ctx = make_ctx(conftest_facts={"tests/conftest.py": facts}, files=files)
    assert (RuleId.COLLECTION_ALTERING_HOOK in fired_rules(evaluate(ctx))) is fires


@pytest.mark.parametrize(
    ("entry", "files", "fires"),
    [
        ("celery.contrib.pytest", ("mypkg/__init__.py",), False),
        ("mypkg.plugins", ("mypkg/__init__.py", "mypkg/plugins.py"), False),
        ("mypkg.plugins", ("mypkg/__init__.py",), True),
    ],
)
def test_r006_pytest_plugins_in_test_module(
    entry: str, files: tuple[str, ...], fires: bool
) -> None:
    path = "tests/test_x.py"
    ctx = make_ctx(
        kinds={path: NodeKind.TEST},
        facts={path: facts_for(path, f"pytest_plugins = [{entry!r}]\n")},
        files=files,
    )
    assert (RuleId.COLLECTION_ALTERING_HOOK in fired_rules(evaluate(ctx))) is fires


def test_r006_pytest_plugins_in_plain_module_is_silent() -> None:
    path = "pkg/mod.py"
    ctx = make_ctx(
        kinds={path: NodeKind.MODULE},
        facts={path: facts_for(path, "pytest_plugins = ['mypkg.plugins']\n")},
        files=("mypkg/__init__.py",),
    )
    assert RuleId.COLLECTION_ALTERING_HOOK not in fired_rules(evaluate(ctx))


# R007 NON_LITERAL_DYNAMIC_IMPORT


def test_r007_non_literal_dynamic_import_taints_the_module() -> None:
    source = "import importlib\n\ndef load(name):\n    return importlib.import_module(name)\n"
    ctx = make_ctx(facts={"pkg/loader.py": facts_for("pkg/loader.py", source)})
    (finding,) = findings_for(evaluate(ctx), RuleId.NON_LITERAL_DYNAMIC_IMPORT)
    assert finding.scope == Scope(ScopeKind.CLOSURE_TAINT, "pkg/loader.py")


def test_r007_literal_dynamic_import_is_silent() -> None:
    source = "import importlib\n\nimportlib.import_module('json')\n"
    ctx = make_ctx(facts={"pkg/loader.py": facts_for("pkg/loader.py", source)})
    assert RuleId.NON_LITERAL_DYNAMIC_IMPORT not in fired_rules(evaluate(ctx))


# R008 SYS_PATH_MUTATION


# Conftests execute unconditionally during collection, so an import-time
# mutation there is global at any nesting depth.
@pytest.mark.parametrize("path", ["conftest.py", "tests/unit/conftest.py"])
def test_r008_import_time_mutation_in_conftest_is_global(path: str) -> None:
    source = "import sys\n\nsys.path.append('vendored')\n"
    ctx = make_ctx(facts={path: facts_for(path, source)})
    (finding,) = findings_for(evaluate(ctx), RuleId.SYS_PATH_MUTATION)
    assert finding.scope == Scope(ScopeKind.GLOBAL)
    assert finding.subject == path


def test_r008_import_time_mutation_in_module_is_global_if_reached() -> None:
    source = "import sys\n\nsys.path.append('vendored')\n"
    ctx = make_ctx(facts={"pkg/paths.py": facts_for("pkg/paths.py", source)})
    (finding,) = findings_for(evaluate(ctx), RuleId.SYS_PATH_MUTATION)
    assert finding.scope == Scope(ScopeKind.GLOBAL_IF_REACHED, "pkg/paths.py")
    assert finding.subject == "pkg/paths.py"


def test_r008_changed_import_time_mutation_in_module_is_global() -> None:
    path = "scripts/paths.py"
    source = "import sys\n\nsys.path.append('vendored')\n"
    ctx = make_ctx(changed=(modified(path),), facts={path: facts_for(path, source)})

    (finding,) = findings_for(evaluate(ctx), RuleId.SYS_PATH_MUTATION)

    assert finding.scope == Scope(ScopeKind.GLOBAL)
    assert "changed and mutates sys.path" in finding.reason


# A function-level mutation runs only if called, so it taints its own module
# everywhere, conftests included: scope edges carry the taint to their tests.
@pytest.mark.parametrize("path", ["pkg/paths.py", "tests/unit/conftest.py"])
def test_r008_function_level_mutation_is_closure_taint(path: str) -> None:
    source = "import sys\n\ndef vendor():\n    sys.path.append('vendored')\n"
    ctx = make_ctx(facts={path: facts_for(path, source)})
    (finding,) = findings_for(evaluate(ctx), RuleId.SYS_PATH_MUTATION)
    assert finding.scope == Scope(ScopeKind.CLOSURE_TAINT, path)
    assert finding.subject == path


def test_r008_both_depths_in_one_module_yield_both_findings() -> None:
    source = "import sys\n\nsys.path.append('a')\n\ndef late():\n    sys.path.append('b')\n"
    ctx = make_ctx(facts={"pkg/paths.py": facts_for("pkg/paths.py", source)})
    findings = findings_for(evaluate(ctx), RuleId.SYS_PATH_MUTATION)
    assert {finding.scope.kind for finding in findings} == {
        ScopeKind.GLOBAL_IF_REACHED,
        ScopeKind.CLOSURE_TAINT,
    }


# The flask blind spot: pytest's monkeypatch fixture mutates sys.path on the
# test's behalf, and a fixture body is a function body, so the runtime kind.
def test_r008_monkeypatch_syspath_prepend_in_conftest_fixture_is_closure_taint() -> None:
    source = """\
import pytest


@pytest.fixture
def test_apps(monkeypatch):
    monkeypatch.syspath_prepend("test_apps")
"""
    path = "tests/conftest.py"
    ctx = make_ctx(facts={path: facts_for(path, source)})
    (finding,) = findings_for(evaluate(ctx), RuleId.SYS_PATH_MUTATION)
    assert finding.scope == Scope(ScopeKind.CLOSURE_TAINT, path)
    assert finding.subject == path


def test_r008_reading_sys_path_is_silent() -> None:
    source = "import sys\n\nknown = list(sys.path)\n"
    ctx = make_ctx(facts={"pkg/paths.py": facts_for("pkg/paths.py", source)})
    assert RuleId.SYS_PATH_MUTATION not in fired_rules(evaluate(ctx))


# R009 EXEC_EVAL


def test_r009_eval_taints_the_module() -> None:
    ctx = make_ctx(facts={"pkg/dyn.py": facts_for("pkg/dyn.py", "eval('1 + 1')\n")})
    (finding,) = findings_for(evaluate(ctx), RuleId.EXEC_EVAL)
    assert finding.scope == Scope(ScopeKind.CLOSURE_TAINT, "pkg/dyn.py")


def test_r009_method_named_eval_is_silent() -> None:
    source = "def run(obj):\n    return obj.eval('x')\n"
    ctx = make_ctx(facts={"pkg/dyn.py": facts_for("pkg/dyn.py", source)})
    assert RuleId.EXEC_EVAL not in fired_rules(evaluate(ctx))


# R010 UNPARSEABLE_FILE


def test_r010_every_unparseable_path_is_reported() -> None:
    ctx = make_ctx(unparseable=("pkg/broken.py", "worse/awful.py"))
    findings = findings_for(evaluate(ctx), RuleId.UNPARSEABLE_FILE)
    assert [finding.subject for finding in findings] == ["pkg/broken.py", "worse/awful.py"]
    assert all(finding.scope.kind is ScopeKind.CLOSURE_TAINT for finding in findings)


def test_r010_parseable_files_are_silent() -> None:
    ctx = make_ctx(facts={"pkg/fine.py": facts_for("pkg/fine.py", "x = 1\n")})
    assert RuleId.UNPARSEABLE_FILE not in fired_rules(evaluate(ctx))


# R011 BROKEN_FIRST_PARTY_IMPORT


@pytest.mark.parametrize(
    "source",
    ["import app.missing\n", "from .missing import thing\n"],
)
def test_r011_broken_first_party_import_in_changed_file(source: str) -> None:
    ctx = make_ctx(
        changed=(modified("app/main.py"),),
        kinds={"app/main.py": NodeKind.MODULE},
        facts={"app/main.py": facts_for("app/main.py", source)},
        files=("app/__init__.py", "app/main.py"),
    )
    (finding,) = findings_for(evaluate(ctx), RuleId.BROKEN_FIRST_PARTY_IMPORT)
    assert finding.scope == Scope(ScopeKind.CLOSURE_TAINT, "app/main.py")
    assert "app.missing" in finding.reason


def test_r011_external_import_is_silent() -> None:
    ctx = make_ctx(
        changed=(modified("app/main.py"),),
        kinds={"app/main.py": NodeKind.MODULE},
        facts={"app/main.py": facts_for("app/main.py", "import requests\n")},
        files=("app/__init__.py", "app/main.py"),
    )
    assert RuleId.BROKEN_FIRST_PARTY_IMPORT not in fired_rules(evaluate(ctx))


def test_r011_unchanged_file_is_silent() -> None:
    ctx = make_ctx(
        kinds={"app/main.py": NodeKind.MODULE},
        facts={"app/main.py": facts_for("app/main.py", "import app.missing\n")},
        files=("app/__init__.py", "app/main.py"),
    )
    assert RuleId.BROKEN_FIRST_PARTY_IMPORT not in fired_rules(evaluate(ctx))


def test_r011_resolvable_first_party_import_is_silent() -> None:
    ctx = make_ctx(
        changed=(modified("app/main.py"),),
        kinds={"app/main.py": NodeKind.MODULE},
        facts={"app/main.py": facts_for("app/main.py", "import app.util\n")},
        files=("app/__init__.py", "app/main.py", "app/util.py"),
    )
    assert RuleId.BROKEN_FIRST_PARTY_IMPORT not in fired_rules(evaluate(ctx))


# R012 LAZY_MODULE_GETATTR


def test_r012_opaque_getattr_assignment_taints_the_module() -> None:
    source = "__getattr__ = make_lazy_hook()\n"
    ctx = make_ctx(facts={"pkg/lazy.py": facts_for("pkg/lazy.py", source)})
    (finding,) = findings_for(evaluate(ctx), RuleId.LAZY_MODULE_GETATTR)
    assert finding.scope == Scope(ScopeKind.CLOSURE_TAINT, "pkg/lazy.py")


def test_r012_def_getattr_with_static_body_is_silent() -> None:
    source = "def __getattr__(name):\n    from pkg.core import thing\n    return thing\n"
    ctx = make_ctx(facts={"pkg/lazy.py": facts_for("pkg/lazy.py", source)})
    assert RuleId.LAZY_MODULE_GETATTR not in fired_rules(evaluate(ctx))


def test_r012_class_getattr_is_silent() -> None:
    source = "class Lazy:\n    def __getattr__(self, name):\n        return None\n"
    ctx = make_ctx(facts={"pkg/lazy.py": facts_for("pkg/lazy.py", source)})
    assert RuleId.LAZY_MODULE_GETATTR not in fired_rules(evaluate(ctx))


# R013 CHANGED_STUB


def test_r013_stub_with_sibling_taints_the_sibling() -> None:
    ctx = make_ctx(
        changed=(modified("pkg/mod.pyi"),),
        kinds={"pkg/mod.pyi": NodeKind.STUB, "pkg/mod.py": NodeKind.MODULE},
    )
    (finding,) = findings_for(evaluate(ctx), RuleId.CHANGED_STUB)
    assert finding.scope == Scope(ScopeKind.CLOSURE_TAINT, "pkg/mod.py")
    assert finding.subject == "pkg/mod.pyi"


def test_r013_orphan_stub_is_global() -> None:
    ctx = make_ctx(changed=(modified("pkg/orphan.pyi"),), kinds={"pkg/orphan.pyi": NodeKind.STUB})
    (finding,) = findings_for(evaluate(ctx), RuleId.CHANGED_STUB)
    assert finding.scope == Scope(ScopeKind.GLOBAL)


def test_r013_plain_module_is_not_a_stub() -> None:
    ctx = make_ctx(changed=(modified("pkg/mod.py"),), kinds={"pkg/mod.py": NodeKind.MODULE})
    assert RuleId.CHANGED_STUB not in fired_rules(evaluate(ctx))


# R014 CHANGED_TEST_FILE


def test_r014_changed_test_is_self_test() -> None:
    ctx = make_ctx(
        changed=(modified("tests/test_api.py"),),
        kinds={"tests/test_api.py": NodeKind.TEST},
    )
    (finding,) = findings_for(evaluate(ctx), RuleId.CHANGED_TEST_FILE)
    assert finding.scope == Scope(ScopeKind.SELF_TEST, "tests/test_api.py")


def test_r014_changed_module_is_silent() -> None:
    ctx = make_ctx(changed=(modified("pkg/mod.py"),), kinds={"pkg/mod.py": NodeKind.MODULE})
    assert RuleId.CHANGED_TEST_FILE not in fired_rules(evaluate(ctx))


def test_r014_deleted_test_file_still_counts() -> None:
    deleted = ChangedFile(path="tests/test_gone.py", status=ChangeStatus.DELETED)
    ctx = make_ctx(changed=(deleted,))
    (finding,) = findings_for(evaluate(ctx), RuleId.CHANGED_TEST_FILE)
    assert finding.scope == Scope(ScopeKind.SELF_TEST, "tests/test_gone.py")


def test_rename_applies_rules_to_the_head_path() -> None:
    renamed = ChangedFile(
        path="tests/test_new.py", status=ChangeStatus.RENAMED, old_path="tests/test_old.py"
    )
    ctx = make_ctx(changed=(renamed,), kinds={"tests/test_new.py": NodeKind.TEST})
    (finding,) = findings_for(evaluate(ctx), RuleId.CHANGED_TEST_FILE)
    assert finding.subject == "tests/test_new.py"


# R015 DOCTEST_MODULES


def test_r015_doctest_modules_fires_globally() -> None:
    ctx = make_ctx(pytest_config=make_pytest_config(doctest_modules=True, source="pyproject.toml"))
    (finding,) = findings_for(evaluate(ctx), RuleId.DOCTEST_MODULES)
    assert finding.scope == Scope(ScopeKind.GLOBAL)
    assert "--doctest-modules" in finding.reason


def test_r015_without_doctest_modules_is_silent() -> None:
    ctx = make_ctx(pytest_config=make_pytest_config(doctest_modules=False))
    assert RuleId.DOCTEST_MODULES not in fired_rules(evaluate(ctx))


# Waivers


def _resource_ctx(waiver: Waiver) -> PolicyContext:
    return make_ctx(
        changed=(modified("assets/logo.png"),),
        kinds={"assets/logo.png": NodeKind.RESOURCE},
        config=AcquitConfig(waivers=(waiver,)),
    )


def test_matching_waiver_moves_finding_to_waived() -> None:
    waiver = Waiver(rule="R001", glob="assets/*", justification="rendered docs only")
    outcome = evaluate(_resource_ctx(waiver))
    assert RuleId.CHANGED_RESOURCE not in fired_rules(outcome)
    (entry,) = outcome.waived
    assert entry.finding.rule is RuleId.CHANGED_RESOURCE
    assert entry.finding.subject == "assets/logo.png"
    assert entry.waiver == waiver


def test_waiver_with_non_matching_glob_leaves_finding_active() -> None:
    waiver = Waiver(rule="R001", glob="docs/*", justification="docs only")
    outcome = evaluate(_resource_ctx(waiver))
    assert RuleId.CHANGED_RESOURCE in fired_rules(outcome)
    assert outcome.waived == ()


def test_waiver_never_applies_to_a_different_rule() -> None:
    waiver = Waiver(rule="R002", glob="assets/*", justification="wrong rule on purpose")
    outcome = evaluate(_resource_ctx(waiver))
    assert RuleId.CHANGED_RESOURCE in fired_rules(outcome)
    assert outcome.waived == ()


# Determinism and completeness


def _rich_ctx(seed: int) -> PolicyContext:
    changed = [
        modified("data/gold.csv"),
        modified("requirements.txt"),
        modified("Dockerfile"),
        modified("native/fast.pyx"),
        modified("pkg/conftest.py"),
        modified("tests/test_a.py"),
        modified("pkg/mod.pyi"),
        modified("app/main.py"),
    ]
    kinds = {
        "data/gold.csv": NodeKind.RESOURCE,
        "requirements.txt": NodeKind.CONFIG,
        "Dockerfile": NodeKind.CONFIG,
        "native/fast.pyx": NodeKind.RESOURCE,
        "pkg/conftest.py": NodeKind.CONFTEST,
        "tests/test_a.py": NodeKind.TEST,
        "pkg/mod.pyi": NodeKind.STUB,
        "pkg/mod.py": NodeKind.MODULE,
        "app/main.py": NodeKind.MODULE,
    }
    loader = "import importlib\n\ndef load(name):\n    return importlib.import_module(name)\n"
    late = "import sys\n\ndef vendor():\n    sys.path.append('late')\n"
    facts = {
        "app/main.py": facts_for("app/main.py", "import app.missing\n"),
        "pkg/dyn.py": facts_for("pkg/dyn.py", loader),
        # All three R008 shapes: conftest import-time, module import-time, runtime.
        "conftest.py": facts_for("conftest.py", "import sys\n\nsys.path.append('vendored')\n"),
        "pkg/paths.py": facts_for("pkg/paths.py", "import sys\n\nsys.path.append('eager')\n"),
        "pkg/latepaths.py": facts_for("pkg/latepaths.py", late),
        "pkg/sh.py": facts_for("pkg/sh.py", "eval('1 + 1')\n"),
        "pkg/lazy.py": facts_for("pkg/lazy.py", "__getattr__ = make_lazy_hook()\n"),
    }
    conftest_facts = {
        "tests/conftest.py": ConftestFacts(
            path="tests/conftest.py",
            collection_altering=("collect_ignore",),
            pytest_plugins=(),
        ),
        "pkg/conftest.py": ConftestFacts(
            path="pkg/conftest.py", collection_altering=(), pytest_plugins=()
        ),
    }
    unparseable = ["bad/broken.py", "worse/awful.py"]
    files = ["app/__init__.py", "app/main.py"]

    rng = random.Random(seed)
    for sequence in (changed, unparseable, files):
        rng.shuffle(sequence)
    kind_items = list(kinds.items())
    fact_items = list(facts.items())
    conftest_items = list(conftest_facts.items())
    for items in (kind_items, fact_items, conftest_items):
        rng.shuffle(items)

    return make_ctx(
        changed=tuple(changed),
        kinds=dict(kind_items),
        facts=dict(fact_items),
        conftest_facts=dict(conftest_items),
        unparseable=tuple(unparseable),
        files=tuple(files),
        pytest_config=make_pytest_config(doctest_modules=True, source="pyproject.toml"),
    )


def test_every_engine_rule_can_fire_in_one_evaluation() -> None:
    engine_rules = {
        RuleId.CHANGED_RESOURCE,
        RuleId.CHANGED_DEPENDENCY_MANIFEST,
        RuleId.CHANGED_TEST_ENVIRONMENT,
        RuleId.CHANGED_NATIVE_SOURCE,
        RuleId.CHANGED_CONFTEST,
        RuleId.COLLECTION_ALTERING_HOOK,
        RuleId.NON_LITERAL_DYNAMIC_IMPORT,
        RuleId.SYS_PATH_MUTATION,
        RuleId.EXEC_EVAL,
        RuleId.UNPARSEABLE_FILE,
        RuleId.BROKEN_FIRST_PARTY_IMPORT,
        RuleId.LAZY_MODULE_GETATTR,
        RuleId.CHANGED_STUB,
        RuleId.CHANGED_TEST_FILE,
        RuleId.DOCTEST_MODULES,
    }
    assert fired_rules(evaluate(_rich_ctx(0))) == engine_rules


def test_findings_are_ordered_by_rule_then_subject() -> None:
    outcome = evaluate(_rich_ctx(0))
    keys = [(finding.rule.value, finding.subject) for finding in outcome.findings]
    assert keys == sorted(keys)


def test_shuffled_inputs_produce_identical_outcomes() -> None:
    outcomes = [evaluate(_rich_ctx(seed)) for seed in range(6)]
    assert all(outcome == outcomes[0] for outcome in outcomes)
