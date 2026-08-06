from libb.client import call


def test_call():
    assert call("k") == "k"
