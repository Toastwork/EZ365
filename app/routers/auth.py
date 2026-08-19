"""Connexion / deconnexion des operateurs EZ365."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..config import get_settings
from ..security import AuthError, authenticate
from ..templating import flash, render

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("operator"):
        return RedirectResponse("/", status_code=303)
    settings = get_settings()
    return render(
        request,
        "login.html",
        {
            "domain": settings.ldap_domain,
            "required_group": settings.ldap_required_group,
            "auth_mode": settings.auth_mode,
        },
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_url: str = Form("/"),
):
    settings = get_settings()
    try:
        operator = authenticate(username.strip(), password)
    except AuthError as exc:
        db.audit(username.strip(), "login.refuse", detail=str(exc))
        return render(
            request,
            "login.html",
            {
                "error": str(exc),
                "username": username,
                "domain": settings.ldap_domain,
                "required_group": settings.ldap_required_group,
                "auth_mode": settings.auth_mode,
            },
            status_code=401,
        )

    request.session["operator"] = operator.as_session()
    db.audit(operator.username, "login.ok")
    log.info("Connexion de %s", operator.username)
    target = next_url if next_url.startswith("/") else "/"
    return RedirectResponse(target, status_code=303)


@router.get("/logout")
async def logout(request: Request):
    operator = request.session.get("operator")
    if operator:
        db.audit(operator["username"], "logout")
    request.session.clear()
    flash(request, "Vous etes deconnecte.", "info")
    return RedirectResponse("/login", status_code=303)
