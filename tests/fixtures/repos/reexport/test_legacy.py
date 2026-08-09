from legacy import Engine


def test_engine_runs() -> None:
    assert Engine().run() == "engine"
