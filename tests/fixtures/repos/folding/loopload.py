from importlib import import_module

LOADED = {}
for name in ("alpha", "beta"):
    LOADED[name] = import_module(name)
