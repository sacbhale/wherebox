"""Step 3 done-when: a 12-item apply-like write transaction runs repeatedly (own thread, own
connection) while 50 concurrent `+1` POSTs hit the app; no SQLITE_BUSY, exact final quantity."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.db.connection import connect, write_tx
from app.inventory import items, search
from tests.inventory_helpers import HX, make_chain, open_db

REQUESTS = 50


def test_apply_transaction_and_50_concurrent_plus_ones(auth_client, auth_settings):
    with open_db(auth_settings) as conn:
        (shelf,) = make_chain(conn, ["Shelf"])
        target = items.create(
            conn, items.ItemData(name="Batteries", location_id=shelf, quantity=5, kind="consumable")
        )

    stop = threading.Event()
    applied: list[int] = []
    writer_errors: list[BaseException] = []

    def apply_loop():
        conn = connect(auth_settings.db_path)
        try:
            while not stop.is_set():
                with write_tx(conn):
                    ids = [
                        items.insert(
                            conn,
                            items.ItemData(
                                name=f"captured {len(applied)}-{n}",
                                location_id=shelf,
                                category="Captured",
                            ),
                        )
                        for n in range(12)
                    ]
                    search.reindex_items(conn, ids)
                    time.sleep(0.01)  # hold the write lock like a real apply would
                applied.append(len(ids))
                # SQLite's lock is not fair: a writer that re-takes it instantly can starve
                # waiters. The real worker makes an AI call (seconds) between applies; 10ms
                # keeps the pressure far higher than that.
                time.sleep(0.01)
        except BaseException as exc:  # reported by the assertion below
            writer_errors.append(exc)
        finally:
            conn.close()

    def plus_one(_):
        return auth_client.post(f"/items/{target}/quantity", data={"delta": "1"}, headers=HX)

    writer = threading.Thread(target=apply_loop)
    writer.start()
    try:
        time.sleep(0.02)  # let the writer take the lock first
        with ThreadPoolExecutor(max_workers=REQUESTS) as pool:
            responses = list(pool.map(plus_one, range(REQUESTS)))
    finally:
        stop.set()
        writer.join(timeout=30)

    assert writer_errors == []
    assert len(applied) >= 2, "the apply loop must overlap the requests"
    statuses = [r.status_code for r in responses]
    assert all(200 <= s < 300 for s in statuses), statuses
    assert not any("locked" in r.text.lower() or "busy" in r.text.lower() for r in responses)
    with open_db(auth_settings) as conn:
        assert items.get(conn, target)["quantity"] == 5 + REQUESTS
        captured = conn.execute(
            "SELECT count(*) FROM items WHERE name LIKE 'captured %'"
        ).fetchone()[0]
        assert captured == 12 * len(applied)
        assert conn.execute("SELECT count(*) FROM items_fts").fetchone()[0] == captured + 1
