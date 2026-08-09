"""Suite-wide isolation: keep the parse cache out of the developer's profile."""

import os
import tempfile

import pytest

from acquit.constants import ENV_CACHE_DIR, ENV_CANARY, ENV_SELECTION_FILE


def pytest_configure(config: object) -> None:
    # The default cache root is the real user cache directory; tests must not
    # write there. Tests that care set their own directory via monkeypatch.
    if ENV_CACHE_DIR not in os.environ:
        os.environ[ENV_CACHE_DIR] = tempfile.mkdtemp(prefix="acquit-test-cache-")


@pytest.fixture(autouse=True)
def _scrub_acquit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Acquit's own CI runs this suite under canary or enforce; the pytest runs
    # the tests spawn must not inherit that posture. The outer plugin already
    # read its environment at session start, so this cannot unhook it.
    monkeypatch.delenv(ENV_SELECTION_FILE, raising=False)
    monkeypatch.delenv(ENV_CANARY, raising=False)
