"""Login-name validation and the case-insensitive comparison form."""

import pytest

from app.auth.passwords import normalize_username, validate_username


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("alice", "alice"),
        ("  Alice  ", "Alice"),  # trimmed, stored as entered
        ("\tBob\n", "Bob"),  # surrounding whitespace (even control) is trimmed first
        ("Anna Maria", "Anna Maria"),  # inner spaces are fine
        ("x", "x"),
        ("x" * 64, "x" * 64),
        ("Zoë", "Zoë"),
    ],
)
def test_valid_names(raw, stored):
    assert validate_username(raw) == stored


@pytest.mark.parametrize(
    "raw",
    [
        *("", "   ", "x" * 65, " " + "x" * 65 + " ", "a\tb", "a\x00b", "bell\x07", "a\x7fb"),
        *("a\x85b", "a\u200bb", "a\u202eb", "a\ufeffb", "a\u2028b"),
    ],
)
def test_invalid_names(raw):
    with pytest.raises(ValueError, match=r"1-64 characters .* no control or invisible characters"):
        validate_username(raw)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Alice", "alice"),
        ("ALICE", "alice"),
        ("  alice ", "alice"),
        ("\uff41\uff4c\uff49\uff43\uff45", "alice"),  # fullwidth letters fold to ASCII under NFKC
        ("Straße", "STRASSE"),  # casefold, not lower
        ("é", "é"),  # composed vs decomposed
    ],
)
def test_equivalent_names(a, b):
    assert normalize_username(a) == normalize_username(b)


@pytest.mark.parametrize(("a", "b"), [("alice", "alicia"), ("alice", "al ice"), ("bob", "b0b")])
def test_different_names(a, b):
    assert normalize_username(a) != normalize_username(b)


@pytest.mark.parametrize(
    "raw", ["\u00a8", "\u02d8", "ß", "Straße", "\uff21\uff22", "\u1e9e", "\u0130"]
)
def test_normalize_is_idempotent(raw):
    once = normalize_username(raw)
    assert normalize_username(once) == once
