import params


def test_load_by_name():
    # A stdlib target keeps the runtime import outside first-party files;
    # the taint on params.py is what pins this test, not the closure.
    assert params.load("json").dumps({}) == "{}"
