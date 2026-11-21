"""Holds the history file's write lock from a second connection, the way a
big prune or restore does."""
from __future__ import annotations

import sqlite3

from bacpack.db import history_db_path


def hold_history_lock(session_factory):
    """Returns a function that releases the lock."""
    main_path = session_factory.kw["bind"].url.database
    connection = sqlite3.connect(str(history_db_path(main_path)), isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")

    def release() -> None:
        connection.execute("ROLLBACK")
        connection.close()

    return release
