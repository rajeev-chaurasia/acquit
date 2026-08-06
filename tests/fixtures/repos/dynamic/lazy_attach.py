def _hook():
    def lookup(name):
        raise AttributeError(name)

    return lookup


__getattr__ = _hook()
ATTACHED_VALUE = 3
