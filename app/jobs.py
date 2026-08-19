"""Execution des taches longues de provisionnement, avec journal consultable.

Chaque tache tourne dans une tache asyncio du processus ; ses evenements sont
ecrits en base pour que l'interface puisse les afficher au fil de l'eau (et
apres un rafraichissement de page). Les etats intermediaires sensibles sont
chiffres avec STORAGE_KEY.
"""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
from typing import Any, Awaitable, Callable

from . import db
from .crypto import decrypt, encrypt

log = logging.getLogger(__name__)

_tasks: dict[str, asyncio.Task] = {}


class JobCancelled(Exception):
    pass


class JobContext:
    """Passe aux orchestrateurs pour journaliser leur progression."""

    def __init__(self, job_id: str, tenant_id: str, actor: str):
        self.job_id = job_id
        self.tenant_id = tenant_id
        self.actor = actor
        self.results: list[dict] = []

    def _event(self, level: str, step: str, message: str, detail: Any = None) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        db.execute(
            "INSERT INTO job_events(job_id, ts, level, step, message, detail) VALUES (?,?,?,?,?,?)",
            (self.job_id, db.now(), level, step, message, detail),
        )
        log.log(
            {"error": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO),
            "[job %s] %s — %s", self.job_id, step, message,
        )

    def info(self, step: str, message: str, detail: Any = None) -> None:
        self._event("info", step, message, detail)

    def success(self, step: str, message: str, detail: Any = None) -> None:
        self._event("success", step, message, detail)

    def warn(self, step: str, message: str, detail: Any = None) -> None:
        self._event("warn", step, message, detail)

    def error(self, step: str, message: str, detail: Any = None) -> None:
        self._event("error", step, message, detail)


def create_job(tenant_id: str, kind: str, actor: str, payload: dict) -> str:
    job_id = db.new_id()
    db.execute(
        "INSERT INTO jobs(id, tenant_id, kind, status, created_by, created_at, payload_enc, summary)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            job_id,
            tenant_id,
            kind,
            "pending",
            actor,
            db.now(),
            encrypt(json.dumps(payload, ensure_ascii=False, default=str)),
            "",
        ),
    )
    return job_id


def get_job(job_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        return None
    job = dict(row)
    job["summary_data"] = json.loads(job["summary"]) if job["summary"] else None
    return job


def job_payload(job_id: str) -> dict:
    row = db.query_one("SELECT payload_enc FROM jobs WHERE id = ?", (job_id,))
    if row is None or not row["payload_enc"]:
        return {}
    return json.loads(decrypt(row["payload_enc"]) or "{}")


def events(job_id: str, after_id: int = 0) -> list[dict]:
    rows = db.query(
        "SELECT id, ts, level, step, message, detail FROM job_events"
        " WHERE job_id = ? AND id > ? ORDER BY id",
        (job_id, after_id),
    )
    return [dict(r) for r in rows]


def recent_jobs(tenant_id: str | None = None, limit: int = 25) -> list[dict]:
    if tenant_id:
        rows = db.query(
            "SELECT j.*, t.display_name AS tenant_name FROM jobs j"
            " LEFT JOIN tenants t ON t.id = j.tenant_id"
            " WHERE j.tenant_id = ? ORDER BY j.created_at DESC LIMIT ?",
            (tenant_id, limit),
        )
    else:
        rows = db.query(
            "SELECT j.*, t.display_name AS tenant_name FROM jobs j"
            " LEFT JOIN tenants t ON t.id = j.tenant_id"
            " ORDER BY j.created_at DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in rows]


def set_summary(job_id: str, summary: dict) -> None:
    db.execute(
        "UPDATE jobs SET summary = ? WHERE id = ?",
        (json.dumps(summary, ensure_ascii=False, default=str), job_id),
    )


def launch(
    job_id: str,
    tenant_id: str,
    actor: str,
    runner: Callable[[JobContext], Awaitable[dict]],
) -> None:
    """Demarre la tache en arriere-plan et tient a jour son statut."""

    async def _wrapper() -> None:
        ctx = JobContext(job_id, tenant_id, actor)
        db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        try:
            summary = await runner(ctx)
            set_summary(job_id, summary)
            db.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                ("done" if not summary.get("has_errors") else "error", db.now(), job_id),
            )
            ctx.success("fin", "Traitement termine.")
        except asyncio.CancelledError:
            db.execute(
                "UPDATE jobs SET status = 'error', finished_at = ? WHERE id = ?",
                (db.now(), job_id),
            )
            ctx.error("fin", "Traitement interrompu.")
            raise
        except Exception as exc:  # noqa: BLE001 - on veut tout tracer dans le journal
            log.exception("Job %s en echec", job_id)
            ctx.error("fin", f"Echec : {exc}", traceback.format_exc(limit=6))
            db.execute(
                "UPDATE jobs SET status = 'error', finished_at = ? WHERE id = ?",
                (db.now(), job_id),
            )
        finally:
            _tasks.pop(job_id, None)

    _tasks[job_id] = asyncio.create_task(_wrapper())


def is_running(job_id: str) -> bool:
    task = _tasks.get(job_id)
    return bool(task and not task.done())


def cancel(job_id: str) -> bool:
    task = _tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


def mark_orphans() -> None:
    """Au demarrage : les jobs 'running' d'un ancien processus sont perdus."""
    db.execute(
        "UPDATE jobs SET status = 'error', finished_at = ?"
        " WHERE status IN ('running','pending')",
        (db.now(),),
    )
