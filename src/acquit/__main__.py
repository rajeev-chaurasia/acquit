"""Make `python -m acquit` work; the study runner invokes acquit this way."""

from acquit.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
