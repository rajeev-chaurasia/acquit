from liba.api import fetch


def call(key):
    return fetch(key)["key"]
