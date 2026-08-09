import registry


def test_load_csv():
    assert registry.load("csv").FORMAT == "csv"


def test_load_json():
    assert registry.load("json").FORMAT == "json"
