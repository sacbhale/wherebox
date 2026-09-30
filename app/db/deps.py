"""Per-request database connection dependency.

Usage in a route: ``def view(conn: Annotated[sqlite3.Connection, Depends(get_conn)])``.
Writes still go through ``write_tx(conn)``. The connection is closed after the response.
"""

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request

from app.db.connection import connect


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
