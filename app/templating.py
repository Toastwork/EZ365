"""Environnement Jinja partage par les routeurs."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime

from fastapi import Request
from fastapi.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def _fr_datetime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%d/%m/%Y %H:%M")


def _asset_version() -> str:
    """Empreinte des fichiers statiques, ajoutee en parametre d'URL.

    Sans elle, le navigateur garde en cache l'ancien CSS/JS apres une mise a
    jour de l'image : l'interface se retrouve a moitie neuve, a moitie ancienne.
    """
    static = os.path.join(BASE_DIR, "static")
    stamps = []
    try:
        for name in sorted(os.listdir(static)):
            stamps.append(str(int(os.path.getmtime(os.path.join(static, name)))))
    except OSError:
        return "0"
    return hashlib.sha1("|".join(stamps).encode()).hexdigest()[:10]


templates.env.filters["dt"] = _fr_datetime
templates.env.globals["app_name"] = "EZ365"
templates.env.globals["asset_v"] = _asset_version()


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    data = dict(context or {})
    data["request"] = request
    operator = request.session.get("operator")
    data.setdefault("operator", operator)
    data.setdefault("flash", request.session.pop("flash", None))
    return templates.TemplateResponse(name, data, status_code=status_code)


def flash(request: Request, message: str, level: str = "info") -> None:
    request.session["flash"] = {"message": message, "level": level}
