"""SQLite cache for Gmail message metadata."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    internal_date INTEGER NOT NULL,
    snippet TEXT,
    subject TEXT,
    from_addr TEXT,
    to_addr TEXT,
    cc_addr TEXT,
    label_ids TEXT NOT NULL,
    size_estimate INTEGER,
    has_attachment INTEGER NOT NULL DEFAULT 0,
    synced_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_internal_date ON messages(internal_date);
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_addr);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(settings.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def upsert_message(
    conn: sqlite3.Connection,
    *,
    id: str,
    thread_id: str,
    internal_date: int,
    snippet: str | None,
    subject: str | None,
    from_addr: str | None,
    to_addr: str | None,
    cc_addr: str | None,
    label_ids: list[str],
    size_estimate: int | None,
    has_attachment: bool,
) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO messages (
            id, thread_id, internal_date, snippet, subject, from_addr, to_addr, cc_addr,
            label_ids, size_estimate, has_attachment, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            thread_id=excluded.thread_id,
            internal_date=excluded.internal_date,
            snippet=excluded.snippet,
            subject=excluded.subject,
            from_addr=excluded.from_addr,
            to_addr=excluded.to_addr,
            cc_addr=excluded.cc_addr,
            label_ids=excluded.label_ids,
            size_estimate=excluded.size_estimate,
            has_attachment=excluded.has_attachment,
            synced_at=excluded.synced_at
        """,
        (
            id,
            thread_id,
            internal_date,
            snippet or "",
            subject or "",
            from_addr or "",
            to_addr or "",
            cc_addr or "",
            json.dumps(label_ids),
            size_estimate,
            1 if has_attachment else 0,
            now,
        ),
    )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if "label_ids" in d and isinstance(d["label_ids"], str):
        try:
            d["label_ids"] = json.loads(d["label_ids"])
        except json.JSONDecodeError:
            d["label_ids"] = []
    return d


def get_kv(conn: sqlite3.Connection, key: str) -> str | None:
    cur = conn.execute("SELECT value FROM kv WHERE key = ?", (key,))
    r = cur.fetchone()
    return r[0] if r else None


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def delete_messages_by_ids(conn: sqlite3.Connection, ids: list[str]) -> int:
    """Remove rows from the local cache (e.g. after Gmail trash)."""
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids)
    return int(cur.rowcount or 0)


def remove_label_from_messages(conn: sqlite3.Connection, ids: list[str], label: str) -> None:
    """Update cached label_ids JSON after archive / read, etc."""
    for mid in ids:
        row = conn.execute("SELECT label_ids FROM messages WHERE id = ?", (mid,)).fetchone()
        if not row:
            continue
        try:
            lids: list[str] = json.loads(row[0] or "[]")
        except json.JSONDecodeError:
            lids = []
        if label not in lids:
            continue
        lids = [x for x in lids if x != label]
        conn.execute(
            "UPDATE messages SET label_ids = ? WHERE id = ?",
            (json.dumps(lids), mid),
        )


def add_label_to_messages(conn: sqlite3.Connection, ids: list[str], label: str) -> None:
    for mid in ids:
        row = conn.execute("SELECT label_ids FROM messages WHERE id = ?", (mid,)).fetchone()
        if not row:
            continue
        try:
            lids = json.loads(row[0] or "[]")
        except json.JSONDecodeError:
            lids = []
        if label in lids:
            continue
        lids = list(lids) + [label]
        conn.execute(
            "UPDATE messages SET label_ids = ? WHERE id = ?",
            (json.dumps(lids), mid),
        )
