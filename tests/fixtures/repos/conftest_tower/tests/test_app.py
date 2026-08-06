import appmod


def test_app(root_token):
    assert root_token == "root"
    assert appmod.value() == 42
