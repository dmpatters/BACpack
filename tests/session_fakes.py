"""A session factory whose commits fail, the way they do when another
process holds the write lock past the busy timeout."""
from __future__ import annotations

import sqlite3

from sqlalchemy.exc import OperationalError


class CommitFailingSessionFactory:
    def __init__(self, inner):
        self.inner = inner
        self.kw = inner.kw

    def __call__(self):
        session = self.inner()

        def fail() -> None:
            raise OperationalError("COMMIT", {}, sqlite3.OperationalError("database is locked"))

        session.commit = fail
        return session
