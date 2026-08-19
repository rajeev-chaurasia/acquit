"""Current release pins stay synchronized across package and user documentation."""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
MARKER = "x-release-please-version"
VERSION = re.compile(r"(?<![0-9])v?(\d+\.\d+\.\d+)(?![0-9])")


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _locked_project_version() -> str:
    with (ROOT / "uv.lock").open("rb") as handle:
        packages = tomllib.load(handle)["package"]
    return next(str(package["version"]) for package in packages if package["name"] == "acquit")


def _runtime_version() -> str:
    source = (ROOT / "src/acquit/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = ["\']([^"\']+)["\']$', source, re.MULTILINE)
    assert match is not None
    return match.group(1)


def _action_default() -> str:
    lines = (ROOT / "action.yml").read_text(encoding="utf-8").splitlines()
    start = lines.index("  acquit-version:")
    for line in lines[start + 1 :]:
        if line.startswith("  ") and not line.startswith("    "):
            break
        match = re.match(r'^    default: ["\']([^"\']+)["\']', line)
        if match is not None:
            return match.group(1)
    raise AssertionError("action.yml has no inputs.acquit-version.default")


def _marked_versions(path: Path) -> tuple[str, ...]:
    versions: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if MARKER not in line:
            continue
        matches = VERSION.findall(line)
        assert len(matches) == 1, f"{path.name}:{number} must mark exactly one current version"
        versions.append(matches[0])
    return tuple(versions)


def test_current_release_versions_match_project_metadata() -> None:
    expected = _project_version()

    assert _locked_project_version() == expected
    assert _runtime_version() == expected
    assert _action_default() == expected
    assert _marked_versions(ROOT / "action.yml") == (expected,)
    assert _marked_versions(ROOT / "README.md") == (expected, expected, expected)
    assert _marked_versions(ROOT / "docs/cli.md") == (expected,)
    assert _marked_versions(ROOT / "uv.lock") == (expected,)


def test_release_please_updates_every_marked_file() -> None:
    config = json.loads((ROOT / "release-please-config.json").read_text(encoding="utf-8"))
    extra_files = {entry["path"] for entry in config["packages"]["."]["extra-files"]}

    assert extra_files >= {"action.yml", "README.md", "docs/cli.md", "uv.lock"}
