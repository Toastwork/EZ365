"""Connexion / deconnexion des operateurs EZ365."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import db, hall_sso
from ..config import get_settings
from ..security import AuthError, Operator, authenticate
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


@router.post("/sso")
async def login_hall(request: Request, jeton: str = Form("")):
    """Connexion unique : jeton signe poste par HALL (voir app/hall_sso.py).

    LDAP_REQUIRED_GROUP s'applique comme pour le formulaire.
    """
    try:
        identite = hall_sso.verifier(jeton, "ez365")
        hall_sso.controler_acces(identite, groupe=get_settings().ldap_required_group)
    except hall_sso.JetonInvalide as exc:
        log.warning("Jeton HALL refuse : %s", exc)
        db.audit("?", "login.hall.refuse", detail=str(exc))
        flash(request, str(exc), "error")
        return RedirectResponse("/login", status_code=303)

    operator = Operator(username=identite["sub"], display_name=identite["name"],
                        groups=identite["groups"])
    request.session.clear()
    request.session["operator"] = operator.as_session()
    db.audit(operator.username, "login.ok", detail="via HALL")
    log.info("Connexion de %s via HALL", operator.username)
    return RedirectResponse("/", status_code=303)

@router.get("/logout")
async def logout(request: Request):
    operator = request.session.get("operator")
    if operator:
        db.audit(operator["username"], "logout")
    request.session.clear()
    flash(request, "Vous etes deconnecte.", "info")
    return RedirectResponse("/login", status_code=303)
