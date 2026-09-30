"""SQLite connections and the single write-transaction helper.

Every write, from web requests and from the worker, goes through ``write_tx``.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from os import PathLike

PRAGMAS = (
    # busy_timeout first: switching to WAL needs a lock, so the busy handler must exist by then.
    "PRAGMA busy_timeout=5000",
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
    # Makes the conflict-resolution DELETE done by REPLACE fire locations_no_delete.
    "PRAGMA recursive_triggers=ON",
)


def connect(path: str | PathLike[str]) -> sqlite3.Connection:
    # isolation_level=None: no implicit transactions; write_tx() issues BEGIN IMMEDIATE itself.
    # check_same_thread=False: FastAPI may open a connection and use it on different threadpool
    # threads within one request; a connection is never used by two threads at once.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE (take the write lock up front), COMMIT on success, ROLLBACK on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
