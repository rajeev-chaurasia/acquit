def __getattr__(name):
    import lazy_target

    return getattr(lazy_target, name)
