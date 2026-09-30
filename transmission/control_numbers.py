# ============================================
# FILE: medical_billing_ai/transmission/control_numbers.py
# PURPOSE: Persistent, monotonically-increasing X12 control numbers
#          (ISA13 / GS06 / ST02). Duplicate interchange control numbers
#          cause clearinghouse rejections -- never hardcode them.
# STORAGE: stdlib sqlite3, survives restarts and is process-safe via
#          IMMEDIATE transactions.
# ============================================

import os
import sqlite3
import threading


class ControlNumberStore:
    def __init__(self, db_path: str = "control_numbers.sqlite3"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS control_counters "
                "(key TEXT PRIMARY KEY, next_value INTEGER NOT NULL)"
            )

    def next(self, key: str, start_at: int = 1) -> int:
        """Returns the next control number for the given counter key,
        e.g. 'ISA13', 'GS06', 'ST02_837'."""
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT next_value FROM control_counters WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    value = start_at
                    conn.execute(
                        "INSERT INTO control_counters (key, next_value) VALUES (?, ?)",
                        (key, value + 1),
                    )
                else:
                    value = row[0]
                    conn.execute(
                        "UPDATE control_counters SET next_value = ? WHERE key = ?",
                        (value + 1, key),
                    )
                conn.commit()
        return value


# Module-level default store (override path via CONTROL_DB_PATH env var)
_default_store = None


def get_default_store() -> ControlNumberStore:
    global _default_store
    if _default_store is None:
        _default_store = ControlNumberStore(
            os.getenv("CONTROL_DB_PATH", "control_numbers.sqlite3")
        )
    return _default_store
