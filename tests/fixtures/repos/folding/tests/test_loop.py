import loopload


def test_loaded_both():
    assert loopload.LOADED["alpha"].ALPHA == 1
    assert loopload.LOADED["beta"].BETA == 2
