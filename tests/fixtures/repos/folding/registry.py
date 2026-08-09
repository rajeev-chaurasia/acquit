from importlib import import_module

_IMPLEMENTATIONS = {"csv": "csv_impl", "json": "json_impl"}


def load(kind):
    return import_module(_IMPLEMENTATIONS[kind])
