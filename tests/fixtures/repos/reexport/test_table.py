from pkg import Table


def test_table_renders() -> None:
    assert Table().render() == "table"
