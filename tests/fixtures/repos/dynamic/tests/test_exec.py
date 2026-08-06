import sys

import execmod


def test_exec_import():
    execmod.run("import exec_target")
    assert "exec_target" in sys.modules
