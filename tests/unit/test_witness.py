"""Unit tests for witness construction and first-principles verification."""

import hashlib

import pytest

from acquit.errors import PolicyError
from acquit.witness import CLAIM_DISJOINT, Witness, build_witness, closure_hash, verify_witness


def test_closure_hash_is_sha256_of_sorted_listing() -> None:
    expected = hashlib.sha256(b"src/a.py\ntests/test_a.py").hexdigest()
    assert closure_hash(["tests/test_a.py", "src/a.py"]) == expected


def test_closure_hash_order_independent() -> None:
    assert closure_hash({"x.py", "y.py"}) == closure_hash(["y.py", "x.py"])


def test_build_witness_success() -> None:
    witness = build_witness(1, "tests/test_a.py", ["tests/test_a.py", "src/a.py"], ["src/b.py"])
    assert witness.id == "w-000001"
    assert witness.test == "tests/test_a.py"
    assert witness.changed == ("src/b.py",)
    assert witness.claim == CLAIM_DISJOINT
    assert witness.closure_hash == closure_hash(["src/a.py", "tests/test_a.py"])


def test_build_witness_refuses_intersection() -> None:
    with pytest.raises(PolicyError, match=r"src/a\.py"):
        build_witness(1, "tests/test_a.py", ["tests/test_a.py", "src/a.py"], ["src/a.py"])


def test_build_witness_sorts_and_dedupes_changed() -> None:
    witness = build_witness(2, "t.py", ["t.py"], ["b.py", "a.py", "b.py"])
    assert witness.changed == ("a.py", "b.py")
    assert witness.id == "w-000002"


def test_build_witness_determinism() -> None:
    first = build_witness(3, "t.py", {"t.py", "u.py"}, {"c.py"})
    second = build_witness(3, "t.py", ["u.py", "t.py"], ("c.py",))
    assert first == second


def test_verify_witness_roundtrip() -> None:
    closure = ["tests/test_a.py", "src/a.py"]
    changed = ["src/b.py", "src/c.py"]
    witness = build_witness(1, "tests/test_a.py", closure, changed)
    assert verify_witness(witness, closure, changed)


def test_verify_witness_rejects_wrong_closure() -> None:
    witness = build_witness(1, "t.py", ["t.py"], ["c.py"])
    assert not verify_witness(witness, ["t.py", "extra.py"], ["c.py"])


def test_verify_witness_rejects_wrong_changed() -> None:
    witness = build_witness(1, "t.py", ["t.py"], ["c.py"])
    assert not verify_witness(witness, ["t.py"], ["d.py"])


def test_verify_witness_rejects_tampered_claim() -> None:
    witness = build_witness(1, "t.py", ["t.py"], ["c.py"])
    forged = Witness(
        id=witness.id,
        test=witness.test,
        closure_hash=witness.closure_hash,
        changed=witness.changed,
        claim="closure looked fine to me",
    )
    assert not verify_witness(forged, ["t.py"], ["c.py"])


def test_verify_witness_rejects_nondisjoint_inputs() -> None:
    witness = build_witness(1, "t.py", ["t.py"], ["c.py"])
    assert not verify_witness(witness, ["t.py"], ["t.py", "c.py"])


def test_verify_witness_rejects_forged_disjoint_claim() -> None:
    # A hand-forged witness over intersecting sets must never verify.
    closure = ["t.py", "src/a.py"]
    changed = ["src/a.py"]
    forged = Witness(
        id="w-000001",
        test="t.py",
        closure_hash=closure_hash(closure),
        changed=tuple(sorted(changed)),
        claim=CLAIM_DISJOINT,
    )
    assert not verify_witness(forged, closure, changed)


def test_empty_changed_set_is_trivially_disjoint() -> None:
    witness = build_witness(1, "t.py", ["t.py"], [])
    assert witness.changed == ()
    assert verify_witness(witness, ["t.py"], [])
