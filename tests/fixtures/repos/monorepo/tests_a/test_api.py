from liba.api import fetch


def test_fetch():
    assert fetch("k") == {"key": "k"}
