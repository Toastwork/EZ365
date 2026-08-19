"""Consentement administrateur multi-tenant + jetons app-only.

Modele : l'application Entra est multi-tenant. Un administrateur du client
accorde le consentement une fois (endpoint /adminconsent) ; ensuite EZ365
obtient des jetons « client credentials » sur le tenant du client, sans
session utilisateur. C'est ce qui permet de provisionner en tache de fond.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from .. import db

log = logging.getLogger(__name__)

LOGIN_HOST = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_cache_lock = threading.Lock()


class ConsentError(Exception):
    pass


def build_consent_url(actor: str) -> str:
    """URL a ouvrir par l'admin du client pour accorder le consentement."""
    settings = get_settings()
    state = secrets.token_urlsafe(24)
    db.execute(
        "INSERT INTO oauth_states(state, actor, created_at) VALUES (?,?,?)",
        (state, actor, db.now()),
    )
    params = {
        "client_id": settings.ms_client_id,
        "redirect_uri": settings.ms_redirect_uri,
        "state": state,
    }
    return f"{LOGIN_HOST}/common/adminconsent?{urlencode(params)}"


def consume_state(state: str) -> str:
    """Valide et consomme un state ; renvoie l'operateur a l'origine du lien."""
    row = db.query_one("SELECT actor, created_at FROM oauth_states WHERE state = ?", (state,))
    if row is None:
        raise ConsentError("Requete de consentement inconnue ou deja utilisee.")
    db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    return row["actor"]


def purge_stale_states(max_age_seconds: int = 3600) -> None:
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
    db.execute("DELETE FROM oauth_states WHERE created_at < ?", (cutoff,))


async def get_app_token(
    tenant_id: str, scope: str = GRAPH_SCOPE, force_refresh: bool = False
) -> str:
    """Jeton applicatif pour un tenant client (mis en cache jusqu'a expiration).

    `scope` permet de viser une autre ressource que Graph, par exemple
    https://contoso.sharepoint.com/.default pour l'API SharePoint REST.
    """
    key = (tenant_id, scope)
    if not force_refresh:
        with _cache_lock:
            cached = _token_cache.get(key)
            if cached and cached[1] > time.time() + 60:
                return cached[0]

    settings = get_settings()
    data = {
        "client_id": settings.ms_client_id,
        "client_secret": settings.ms_client_secret,
        "grant_type": "client_credentials",
        "scope": scope,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{LOGIN_HOST}/{tenant_id}/oauth2/v2.0/token", data=data)

    if resp.status_code != 200:
        payload = _safe_json(resp)
        code = payload.get("error", "")
        desc = payload.get("error_description", resp.text[:300])
        if "AADSTS7000215" in desc:
            raise ConsentError(
                "Secret client invalide : regenerez MS_CLIENT_SECRET dans Azure et "
                "mettez-le a jour dans le compose."
            )
        if code == "invalid_client" or "AADSTS700016" in desc:
            raise ConsentError(
                "Le consentement administrateur n'a pas ete accorde sur ce tenant "
                "(ou il a ete revoque). Relancez « Connecter un tenant »."
            )
        raise ConsentError(f"Echec d'obtention du jeton ({code}) : {desc}")

    payload = resp.json()
    token = payload["access_token"]
    expires_at = time.time() + int(payload.get("expires_in", 3600))
    with _cache_lock:
        _token_cache[key] = (token, expires_at)
    return token


def invalidate(tenant_id: str) -> None:
    with _cache_lock:
        for key in [k for k in _token_cache if k[0] == tenant_id]:
            _token_cache.pop(key, None)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}
