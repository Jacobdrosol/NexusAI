import sqlite3

import pytest

from control_plane.sqlite_helpers import SQLITE_BUSY_TIMEOUT_MS, open_sqlite


@pytest.mark.anyio
async def test_open_sqlite_configures_busy_timeout_wal_and_foreign_keys(tmp_path):
    db_path = tmp_path / "sqlite-helper.db"

    async with open_sqlite(str(db_path), foreign_keys=True) as db:
        async with db.execute("PRAGMA busy_timeout") as cursor:
            busy_timeout = await cursor.fetchone()
        async with db.execute("PRAGMA journal_mode") as cursor:
            journal_mode = await cursor.fetchone()
        async with db.execute("PRAGMA foreign_keys") as cursor:
            foreign_keys = await cursor.fetchone()

    assert int(busy_timeout[0]) == SQLITE_BUSY_TIMEOUT_MS
    assert str(journal_mode[0]).lower() == "wal"
    assert int(foreign_keys[0]) == 1

    # Confirm WAL mode persists for future raw SQLite connections too.
    conn = sqlite3.connect(str(db_path))
    try:
        journal_mode_check = conn.execute("PRAGMA journal_mode").fetchone()
        assert str(journal_mode_check[0]).lower() == "wal"
    finally:
        conn.close()
