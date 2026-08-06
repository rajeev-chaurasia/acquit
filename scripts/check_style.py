"""House style checks that ruff does not cover. Currently: no em-dashes anywhere."""

import pathlib

CHECKED_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json"}
EM_DASH = "\u2014"


def main() -> int:
    offenders: list[str] = []
    for path in pathlib.Path(".").rglob("*"):
        if ".git" in path.parts or ".venv" in path.parts:
            continue
        if not path.is_file() or path.suffix not in CHECKED_SUFFIXES:
            continue
        if EM_DASH in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(path.as_posix())
    if offenders:
        print("em-dash found in: " + ", ".join(sorted(offenders)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
