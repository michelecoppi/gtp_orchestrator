"""Stato su SQLite: sviluppo, replay ed evaluation in locale.

Una sola tabella documentale. `_atomic()` apre una transazione `BEGIN IMMEDIATE`: due processi
sullo stesso file si escludono a vicenda, come due transazioni Firestore.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from supervisor.state.store import DocStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    collection TEXT NOT NULL,
    id TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL,
    PRIMARY KEY (collection, id)
);
CREATE INDEX IF NOT EXISTS documents_ts ON documents (collection, ts);
"""


class SQLiteStore(DocStore):
    def __init__(self, path: str, timeout: float = 30.0) -> None:
        self.path = path
        self._conn = sqlite3.connect(path, timeout=timeout, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._thread_lock = threading.RLock()
        self._depth = 0

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _atomic(self):
        with self._thread_lock:
            outermost = self._depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outermost:
                self._conn.execute("COMMIT")

    def _get(self, collection, doc_id):
        with self._thread_lock:
            row = self._conn.execute(
                "SELECT data FROM documents WHERE collection = ? AND id = ?", (collection, doc_id)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, collection, doc_id, doc, ts=""):
        with self._thread_lock:
            self._conn.execute(
                "INSERT INTO documents (collection, id, ts, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (collection, id) DO UPDATE SET ts = excluded.ts, data = excluded.data",
                (collection, doc_id, ts, json.dumps(doc, ensure_ascii=False, sort_keys=True)),
            )

    def _delete(self, collection, doc_id):
        with self._thread_lock:
            self._conn.execute("DELETE FROM documents WHERE collection = ? AND id = ?", (collection, doc_id))

    def _scan(self, collection, ts_from=None):
        with self._thread_lock:
            if ts_from is None:
                rows = self._conn.execute("SELECT data FROM documents WHERE collection = ?", (collection,))
            else:
                rows = self._conn.execute(
                    "SELECT data FROM documents WHERE collection = ? AND ts >= ?", (collection, ts_from)
                )
            return [json.loads(r[0]) for r in rows.fetchall()]
