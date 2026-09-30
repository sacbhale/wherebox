import sqlite3

import pytest

from app.inventory import shortcode


def test_generate_uses_crockford_alphabet():
    codes = {shortcode.generate() for _ in range(2000)}
    assert len(codes) > 1990  # 32^6 space: collisions here would mean broken randomness
    for code in codes:
        assert len(code) == 6
        assert set(code) <= set(shortcode.ALPHABET)
    assert not set("ILOU") & set(shortcode.ALPHABET)
    assert len(shortcode.ALPHABET) == 32


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc123", "ABC123"),
        ("ab-c1-23", "ABC123"),
        ("  7k9q2z ", "7K9Q2Z"),
        ("oOiIlL", "001111"),
        ("0O1IL-", "00111"),
    ],
)
def test_normalize(raw, expected):
    assert shortcode.normalize(raw) == expected


def test_normalize_is_identity_on_generated_codes():
    for _ in range(200):
        code = shortcode.generate()
        assert shortcode.normalize(code) == code


@pytest.fixture
def table():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, short_code TEXT NOT NULL UNIQUE, n TEXT)")
    conn.execute("INSERT INTO t (short_code, n) VALUES ('AAAAAA', 'taken')")
    yield conn
    conn.close()


SQL = "INSERT INTO t (short_code, n) VALUES (:short_code, :n)"


def test_insert_retries_on_unique_conflict(table, monkeypatch):
    codes = iter(["AAAAAA", "AAAAAA", "BBBBBB"])
    monkeypatch.setattr(shortcode, "generate", lambda: next(codes))
    rowid, code = shortcode.insert_with_code(table, SQL, {"n": "new"})
    assert code == "BBBBBB"
    assert table.execute("SELECT short_code, n FROM t WHERE id = ?", (rowid,)).fetchone() == (
        "BBBBBB",
        "new",
    )


def test_insert_gives_up_after_max_attempts(table, monkeypatch):
    calls = []
    monkeypatch.setattr(shortcode, "generate", lambda: calls.append(1) or "AAAAAA")
    with pytest.raises(sqlite3.IntegrityError, match="short_code"):
        shortcode.insert_with_code(table, SQL, {"n": "new"})
    assert len(calls) == shortcode.MAX_ATTEMPTS == 5


def test_insert_does_not_retry_other_integrity_errors(table, monkeypatch):
    calls = []
    monkeypatch.setattr(shortcode, "generate", lambda: calls.append(1) or "CCCCCC")
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        shortcode.insert_with_code(table, "INSERT INTO t (short_code) VALUES (NULL)", {})
    assert len(calls) == 1
