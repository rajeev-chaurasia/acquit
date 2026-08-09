from pkg import Console


def test_console_banner() -> None:
    assert Console().banner() == "console*"
