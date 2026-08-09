from pathlib import Path
from textwrap import dedent

import pytest

from acquit.config import AcquitConfig, Waiver, load_config
from acquit.errors import PolicyError

VALID_WAIVER = Waiver(rule="R009", glob="src/legacy/**", justification="audited template exec")


def _write(path: Path, body: str) -> None:
    path.write_text(dedent(body), encoding="utf-8")


def test_defaults_when_no_config_exists(tmp_path: Path) -> None:
    assert load_config(tmp_path) == AcquitConfig()


def test_acquit_toml_top_level_keys(tmp_path: Path) -> None:
    _write(
        tmp_path / ".acquit.toml",
        """
        roots = ["src", "lib"]
        assume_inert = ["docs/**"]

        [[waive]]
        rule = "R009"
        glob = "src/legacy/**"
        justification = "audited template exec"
        """,
    )
    config = load_config(tmp_path)
    assert config.roots == ("src", "lib")
    assert config.assume_inert == ("docs/**",)
    assert config.waivers == (VALID_WAIVER,)


def test_pyproject_tool_acquit_section(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml",
        """
        [project]
        name = "demo"

        [tool.acquit]
        roots = ["src"]

        [[tool.acquit.waive]]
        rule = "R009"
        glob = "src/legacy/**"
        justification = "audited template exec"
        """,
    )
    config = load_config(tmp_path)
    assert config.roots == ("src",)
    assert config.waivers == (VALID_WAIVER,)


def test_acquit_toml_beats_pyproject(tmp_path: Path) -> None:
    _write(tmp_path / ".acquit.toml", 'roots = ["from-acquit-toml"]\n')
    _write(
        tmp_path / "pyproject.toml",
        """
        [tool.acquit]
        roots = ["from-pyproject"]
        """,
    )
    assert load_config(tmp_path).roots == ("from-acquit-toml",)


def test_pyproject_without_section_gives_defaults(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml",
        """
        [project]
        name = "demo"
        """,
    )
    assert load_config(tmp_path) == AcquitConfig()


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        pytest.param(
            """
            [[waive]]
            glob = "**"
            justification = "reason"
            """,
            "missing key(s): rule",
            id="missing-rule",
        ),
        pytest.param(
            """
            [[waive]]
            rule = "R009"
            justification = "reason"
            """,
            "missing key(s): glob",
            id="missing-glob",
        ),
        pytest.param(
            """
            [[waive]]
            rule = "R009"
            glob = "**"
            """,
            "missing key(s): justification",
            id="missing-justification",
        ),
        pytest.param(
            """
            [[waive]]
            rule = "R009"
            glob = "**"
            justification = ""
            """,
            "justification must not be empty",
            id="empty-justification",
        ),
        pytest.param(
            """
            [[waive]]
            rule = "R009"
            glob = "**"
            justification = 3
            """,
            "'justification' must be a string",
            id="non-string-justification",
        ),
        pytest.param(
            'waive = ["oops"]\n',
            "must be a table",
            id="non-table-entry",
        ),
        pytest.param(
            """
            [[waive]]
            rule = "R009"
            glob = "**"
            justification = "reason"
            expires = "2027-01-01"
            """,
            "unknown key(s): expires",
            id="unknown-waiver-key",
        ),
    ],
)
def test_invalid_waiver_names_entry(tmp_path: Path, body: str, fragment: str) -> None:
    _write(tmp_path / ".acquit.toml", body)
    with pytest.raises(PolicyError, match="waive entry 1") as excinfo:
        load_config(tmp_path)
    assert fragment in str(excinfo.value)


def test_narrowing_defaults_to_disabled(tmp_path: Path) -> None:
    _write(tmp_path / ".acquit.toml", 'roots = ["src"]\n')
    assert load_config(tmp_path).narrowing is False
    assert AcquitConfig().narrowing is False


def test_narrowing_flag_parses_from_acquit_toml(tmp_path: Path) -> None:
    _write(tmp_path / ".acquit.toml", "narrowing = true\n")
    assert load_config(tmp_path).narrowing is True


def test_narrowing_flag_parses_from_pyproject(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml",
        """
        [tool.acquit]
        narrowing = true
        """,
    )
    assert load_config(tmp_path).narrowing is True


def test_non_boolean_narrowing_raises(tmp_path: Path) -> None:
    _write(tmp_path / ".acquit.toml", 'narrowing = "yes"\n')
    with pytest.raises(PolicyError, match="'narrowing' must be a boolean"):
        load_config(tmp_path)


def test_unknown_top_level_key_raises(tmp_path: Path) -> None:
    _write(tmp_path / ".acquit.toml", 'rootz = ["src"]\n')
    with pytest.raises(PolicyError, match="rootz"):
        load_config(tmp_path)


def test_unknown_key_in_pyproject_section_raises(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml",
        """
        [tool.acquit]
        assume_inart = ["docs/**"]
        """,
    )
    with pytest.raises(PolicyError, match="assume_inart"):
        load_config(tmp_path)


def test_non_array_roots_raises(tmp_path: Path) -> None:
    _write(tmp_path / ".acquit.toml", 'roots = "src"\n')
    with pytest.raises(PolicyError, match="'roots' must be an array of strings"):
        load_config(tmp_path)


def test_invalid_toml_raises_policy_error(tmp_path: Path) -> None:
    _write(tmp_path / ".acquit.toml", "roots = [\n")
    with pytest.raises(PolicyError, match="invalid TOML"):
        load_config(tmp_path)
