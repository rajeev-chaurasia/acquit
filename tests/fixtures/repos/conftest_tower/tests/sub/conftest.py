import pytest
import tower_helpers


@pytest.fixture
def sub_token():
    return "sub-" + tower_helpers.suffix()
