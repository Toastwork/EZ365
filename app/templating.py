"""Environnement Jinja partage par les routeurs."""
from __future__ import annotations

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


templates.env.filters["dt"] = _fr_datetime
templates.env.globals["app_name"] = "EZ365"


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    data = dict(context or {})
    data["request"] = request
    operator = request.session.get("operator")
    data.setdefault("operator", operator)
    data.setdefault("flash", request.session.pop("flash", None))
    return templates.TemplateResponse(name, data, status_code=status_code)


def flash(request: Request, message: str, level: str = "info") -> None:
    request.session["flash"] = {"message": message, "level": level}
