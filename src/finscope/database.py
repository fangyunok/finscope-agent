"""Transactional confirmed memory; no process-global personal-data cache."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 user_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, display_name TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, memory_epoch INTEGER NOT NULL DEFAULT 0,
 profile_version INTEGER NOT NULL DEFAULT 0, feedback_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS profiles (
 owner_id TEXT PRIMARY KEY REFERENCES users(user_id), data_json TEXT NOT NULL,
 version INTEGER NOT NULL, memory_epoch INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS field_sources (
 owner_id TEXT NOT NULL REFERENCES users(user_id), field TEXT NOT NULL,
 value_json TEXT NOT NULL, event_id TEXT NOT NULL, source TEXT NOT NULL,
 confirmed_version INTEGER NOT NULL, confirmed_at TEXT NOT NULL,
 PRIMARY KEY(owner_id, field)
);
CREATE TABLE IF NOT EXISTS profile_history (
 owner_id TEXT NOT NULL REFERENCES users(user_id), version INTEGER NOT NULL,
 memory_epoch INTEGER NOT NULL, data_json TEXT NOT NULL, event_id TEXT NOT NULL,
 confirmed_at TEXT NOT NULL, PRIMARY KEY(owner_id, version)
);
CREATE TABLE IF NOT EXISTS proposals (
 proposal_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id),
 memory_epoch INTEGER NOT NULL, base_version INTEGER NOT NULL,
 changes_json TEXT NOT NULL, candidate_json TEXT NOT NULL, content_hash TEXT NOT NULL,
 source TEXT NOT NULL, status TEXT NOT NULL, confirmed_version INTEGER,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
 owner_id TEXT NOT NULL REFERENCES users(user_id), product_id TEXT NOT NULL,
 action TEXT NOT NULL, memory_epoch INTEGER NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(owner_id, product_id)
);
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id),
 memory_epoch INTEGER NOT NULL, profile_version INTEGER NOT NULL,
 mode TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL,
 request_id TEXT, request_hash TEXT NOT NULL, result_json TEXT,
 created_at TEXT NOT NULL, completed_at TEXT, feedback_version INTEGER NOT NULL DEFAULT 0,
 UNIQUE(owner_id, request_id)
);
CREATE TABLE IF NOT EXISTS events (
 event_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id),
 kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.executescript(SCHEMA)
            for table in ("users", "runs"):
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                if "feedback_version" not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN feedback_version INTEGER NOT NULL DEFAULT 0")
        finally:
            connection.close()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def transaction(self, *, write=False):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
