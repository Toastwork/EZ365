"""Persistance SQLite dans /data (volume monte par le compose)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import get_settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id              TEXT PRIMARY KEY,          -- tenant id Entra (GUID)
    display_name    TEXT NOT NULL DEFAULT '',
    default_domain  TEXT NOT NULL DEFAULT '',
    consented_by    TEXT NOT NULL DEFAULT '',
    consented_at    TEXT NOT NULL,
    last_checked_at TEXT,
    status          TEXT NOT NULL DEFAULT 'ok',
    vault_org_id    TEXT,
    vault_collection_id TEXT,
    notes           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,               -- pending | running | done | error
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    finished_at  TEXT,
    payload_enc  TEXT,                        -- parametres, chiffres (mots de passe)
    summary      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS job_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT NOT NULL,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL,                  -- info | warn | error | success
    step      TEXT NOT NULL,
    message   TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    target   TEXT NOT NULL DEFAULT '',
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);

CREATE TABLE IF NOT EXISTS oauth_states (
    state      TEXT PRIMARY KEY,
    actor      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            settings = get_settings()
            os.makedirs(settings.data_dir, exist_ok=True)
            _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    conn = connect()
    with _lock:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    conn = connect()
    with _lock:
        return conn.execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def audit(actor: str, action: str, target: str = "", detail: Any = "") -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    execute(
        "INSERT INTO audit(ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
        (now(), actor, action, target, detail),
    )
