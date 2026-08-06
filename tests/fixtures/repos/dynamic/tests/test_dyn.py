import dynloader


def test_dyn_load():
    module = dynloader.load("extra")
    assert module.EXTRA == "extra"
