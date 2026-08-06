"""Suite-wide isolation: keep the parse cache out of the developer's profile."""

import os
import tempfile

from acquit.constants import ENV_CACHE_DIR


def pytest_configure(config: object) -> None:
    # The default cache root is the real user cache directory; tests must not
    # write there. Tests that care set their own directory via monkeypatch.
    if ENV_CACHE_DIR not in os.environ:
        os.environ[ENV_CACHE_DIR] = tempfile.mkdtemp(prefix="acquit-test-cache-")
