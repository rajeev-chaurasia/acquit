import lazy_attach


def test_attached_value():
    assert lazy_attach.ATTACHED_VALUE == 3
