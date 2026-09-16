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

-- Sites crees par EZ365 ou designes a la main par leur adresse. Un site tout
-- juste cree peut manquer quelques minutes a l'enumeration Graph : on les
-- retient pour les proposer sans attendre.
CREATE TABLE IF NOT EXISTS sites (
    tenant_id    TEXT NOT NULL,
    site_id      TEXT NOT NULL,
    web_url      TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    origin       TEXT NOT NULL DEFAULT 'ez365',   -- ez365 | manuel
    created_at   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, site_id)
);

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


def remember_site(tenant_id: str, site: dict, origin: str = "ez365") -> None:
    """Retient un site pour le proposer sans attendre l'index de recherche."""
    if not site or not site.get("id"):
        return
    execute(
        "INSERT INTO sites(tenant_id, site_id, web_url, display_name, origin, created_at)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(tenant_id, site_id) DO UPDATE SET"
        " web_url = excluded.web_url, display_name = excluded.display_name",
        (
            tenant_id,
            site["id"],
            site.get("webUrl") or "",
            site.get("displayName") or site.get("name") or "",
            origin,
            now(),
        ),
    )


def remembered_sites(tenant_id: str) -> list[dict]:
    rows = query(
        "SELECT site_id, web_url, display_name, origin FROM sites"
        " WHERE tenant_id = ? ORDER BY created_at DESC",
        (tenant_id,),
    )
    return [
        {
            "id": r["site_id"],
            "webUrl": r["web_url"],
            "displayName": r["display_name"],
            "origin": r["origin"],
        }
        for r in rows
    ]


def audit(actor: str, action: str, target: str = "", detail: Any = "") -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    execute(
        "INSERT INTO audit(ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
        (now(), actor, action, target, detail),
    )
