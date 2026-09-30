from app.db import migrate as migrate_module
from app.db.connection import connect
from app.db.migrate import migrate
from app.inventory import shortcode

TABLES = {
    "locations",
    "categories",
    "items",
    "photos",
    "captures",
    "ai_calls",
    "review_proposals",
    "sessions",
    "settings",
    "items_fts",
    "schema_migrations",
}
LATEST = sorted(p.stem for p in migrate_module.MIGRATIONS_DIR.glob("*.sql"))[-1]


def names(conn, kind):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    return {r[0] for r in rows}


def test_fresh_migrate_applies_all_then_is_idempotent(settings):
    conn = connect(settings.db_path)
    try:
        assert migrate(conn)[-1] == LATEST
        codes = conn.execute("SELECT id, short_code FROM locations").fetchall()
        schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        assert migrate(conn) == []
        assert migrate(conn) == []
        assert conn.execute("SELECT id, short_code FROM locations").fetchall() == codes
        after = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        assert after == schema
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        assert versions == sorted(p.stem for p in migrate_module.MIGRATIONS_DIR.glob("*.sql"))
    finally:
        conn.close()


def test_idempotent_across_connections(settings):
    for _ in range(3):
        conn = connect(settings.db_path)
        migrate(conn)
        conn.close()
    conn = connect(settings.db_path)
    assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == len(
        list(migrate_module.MIGRATIONS_DIR.glob("*.sql"))
    )
    conn.close()


def test_all_tables_present(conn):
    assert names(conn, "table") >= TABLES
    assert "one_open_proposal" in names(conn, "index")


def test_both_short_code_triggers_present_after_latest_migration(conn):
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == LATEST
    assert {"locations_code_immutable", "locations_no_delete"} <= names(conn, "trigger")
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'locations_code_immutable'"
    ).fetchone()[0]
    assert "OLD.short_code IS NOT NEW.short_code" in sql


def test_inbox_exactly_one(conn):
    rows = conn.execute(
        "SELECT name, short_code, parent_id FROM locations WHERE is_system = 1"
    ).fetchall()
    assert len(rows) == 1
    name, code, parent_id = rows[0]
    assert (name, parent_id) == ("Inbox", None)
    assert len(code) == 6 and set(code) <= set(shortcode.ALPHABET)
    migrate(conn)
    assert conn.execute("SELECT count(*) FROM locations WHERE is_system = 1").fetchone()[0] == 1


def test_connection_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    assert conn.isolation_level is None


def test_items_fts_is_regular_fts5_table(conn):
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'items_fts'").fetchone()[0]
    assert "fts5" in sql and "content=" not in sql.replace(" ", "")
    assert not {t for t in names(conn, "trigger") if "fts" in t}
    conn.execute(
        "INSERT INTO items_fts (rowid, name, category, notes, location_path) "
        "VALUES (7, 'AA Batteries', 'Electronics', '', 'Garage > Shelf 2')"
    )
    hit = conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH '\"batt\"*'").fetchone()
    assert hit[0] == 7
