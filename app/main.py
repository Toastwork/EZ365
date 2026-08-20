"""Point d'entree FastAPI d'EZ365."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db, jobs
from .config import get_settings
from .crypto import session_secret
from .logging_conf import configure_logging
from .msgraph import oauth
from .routers import auth as auth_router
from .routers import provisioning as provisioning_router
from .routers import tenants as tenants_router
from .templating import render
from .vault import bitwarden

log = logging.getLogger(__name__)

PUBLIC_PATHS = {"/login", "/logout", "/ms/callback", "/healthz", "/favicon.ico"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

    missing = settings.missing()
    if missing:
        log.error("Variables d'environnement manquantes : %s", ", ".join(missing))

    db.connect()
    jobs.mark_orphans()
    oauth.purge_stale_states()
    log.info(
        "EZ365 %s demarre — auth=%s, coffre=%s, TLS=%s, donnees=%s",
        settings.build_ref,
        settings.auth_mode,
        "actif" if settings.vault_enabled else "inactif",
        "oui" if settings.tls_enabled else "non",
        settings.data_dir,
    )
    yield
    log.info("EZ365 s'arrete.")


app = FastAPI(title="EZ365", docs_url=None, redoc_url=None, lifespan=lifespan)

_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    is_public = path in PUBLIC_PATHS or path.startswith("/static/")
    if not is_public and not request.session.get("operator"):
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"error": "Session expiree"}, status_code=401)
        target = "/login"
        if path != "/":
            target = f"/login?next={path}"
        return RedirectResponse(target, status_code=303)
    return await call_next(request)


# Starlette empile les middlewares dans l'ordre inverse d'ajout : SessionMiddleware
# doit etre enregistre EN DERNIER pour envelopper require_login, qui lit la session.
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    session_cookie="ez365_session",
    https_only=get_settings().tls_enabled,
    same_site="lax",
    max_age=8 * 3600,
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return render(
        request,
        "error.html",
        {"code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
    )


@app.get("/healthz")
async def healthz():
    settings = get_settings()
    vault_ready, vault_message = (False, "desactive")
    if settings.vault_enabled:
        vault_ready, vault_message = await bitwarden.is_ready()
    try:
        db.query_one("SELECT 1 AS ok")
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        log.error("Base de donnees inaccessible : %s", exc)

    healthy = db_ok and not settings.missing()
    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "database": db_ok,
            "vault": {"enabled": settings.vault_enabled, "ready": vault_ready,
                      "detail": vault_message},
            "build": settings.build_ref,
            "config_missing": settings.missing(),
        },
        status_code=200 if healthy else 503,
    )


app.include_router(auth_router.router)
app.include_router(tenants_router.router)
app.include_router(provisioning_router.router)


def main() -> None:
    import uvicorn

    settings = get_settings()
    configure_logging()
    kwargs: dict = {
        "host": settings.host,
        "port": settings.port,
        "log_level": settings.log_level.lower(),
        "proxy_headers": True,
        "forwarded_allow_ips": "*",
    }
    if settings.tls_enabled:
        kwargs["ssl_certfile"] = settings.ssl_certfile
        kwargs["ssl_keyfile"] = settings.ssl_keyfile
        if settings.ssl_keyfile_password:
            kwargs["ssl_keyfile_password"] = settings.ssl_keyfile_password
    uvicorn.run("app.main:app", **kwargs)


if __name__ == "__main__":
    main()
