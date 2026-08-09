from obs import Gadget


def test_gadget_spins() -> None:
    assert Gadget().spin() == "gadget"
