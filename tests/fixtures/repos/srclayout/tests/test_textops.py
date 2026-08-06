from pkg.textops import shout


def test_shout():
    assert shout("hi") == "HI"
