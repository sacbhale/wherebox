"""FTS query building: user text can never produce an FTS5 syntax error."""

import random
import sqlite3

import pytest

from app.inventory import items, search
from tests.inventory_helpers import make_chain, seed_items


def test_tokens_become_quoted_prefix_phrases():
    assert search.build_fts_query("aa batt") == '"aa"* "batt"*'
    assert search.build_fts_query('say "hi"') == '"say"* """hi"""*'
    assert search.build_fts_query("  \t ") is None
    assert search.build_fts_query("") is None


@pytest.fixture
def fts() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE items_fts USING fts5(name, category, notes, location_path, "
        "tokenize = 'unicode61 remove_diacritics 2')"
    )
    conn.executemany(
        "INSERT INTO items_fts (rowid, name, category, notes, location_path) VALUES (?,?,?,?,?)",
        [
            (1, "AA batteries", "Electronics", "", "Garage › Shelf 2 › Blue box"),
            (2, "Café crème", "Food", 'the "good" one', "Kitchen"),
            (3, "AND gate", "", "NOT OR NEAR", "Lab"),
        ],
    )
    return conn


SPECIALS = ['"', "'", "*", "(", ")", "^", ":", "-", "+", "{", "}", "[", "]", ",", ".", "\\", "/"]
WORDS = ["AND", "OR", "NOT", "NEAR", "NEAR(", "batt", "café", "rowid", "name:", "*(", "\"'", ""]


def _random_query(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(1, 6)):
        choice = rng.random()
        if choice < 0.4:
            parts.append("".join(rng.choice(SPECIALS) for _ in range(rng.randint(1, 4))))
        elif choice < 0.8:
            parts.append(rng.choice(WORDS))
        else:
            parts.append("".join(chr(rng.randint(33, 0x2FF)) for _ in range(rng.randint(1, 5))))
    joiner = rng.choice([" ", "", "  ", "\t"])
    return joiner.join(parts)


def test_fuzzed_queries_never_raise(fts):
    rng = random.Random(1234)
    fixed = ['"', "AND", "*(", "OR", "NOT", 'a"b', "(", ")", "NEAR(a b)", "col:val", "^x", '""']
    for q in fixed + [_random_query(rng) for _ in range(3000)]:
        match = search.build_fts_query(q)
        if match is None:
            continue
        fts.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH ?", (match,)).fetchall()


def test_operators_are_literal_words(fts):
    def hits(q):
        match = search.build_fts_query(q)
        return [
            r[0]
            for r in fts.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH ?", (match,))
        ]

    assert hits("batt") == [1]
    assert hits("AND") == [3]
    assert hits("cafe") == [2]  # diacritics removed
    assert hits("shelf 2") == [1]
    assert hits('"good"') == [2]


def test_search_function_handles_odd_input(conn):
    (box,) = make_chain(conn, ["Box"])
    seed_items(conn, [items.ItemData(name="AA batteries", location_id=box)])
    for q in ['"', "AND", "*(", "NOT(", 'bat"t', "\\"]:
        search.search(conn, q)
    assert [r["name"] for r in search.search(conn, "AA batt")] == ["AA batteries"]
