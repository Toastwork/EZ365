"""Client de la Vault Management API exposee par le sidecar `bw serve`.

Le conteneur ez365-bw-cli se connecte au Vaultwarden, se deverrouille avec
BW_PASSWORD puis publie l'API sur http://bw-cli:8087. EZ365 ne connait donc
jamais le mot de passe maitre : il parle a une session deja ouverte.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

TYPE_LOGIN = 1
TYPE_SECURE_NOTE = 2


class VaultError(Exception):
    pass


class VaultDisabled(VaultError):
    pass


def _base_url() -> str:
    settings = get_settings()
    if not settings.vault_enabled:
        raise VaultDisabled("Le coffre est desactive (VAULT_ENABLED=false).")
    if not settings.vault_api_url:
        raise VaultDisabled("VAULT_API_URL n'est pas defini.")
    return settings.vault_api_url


async def _call(method: str, path: str, *, json: Any = None, params: dict | None = None) -> Any:
    url = f"{_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.request(method, url, json=json, params=params)
    except httpx.HTTPError as exc:
        raise VaultError(
            f"Sidecar Bitwarden injoignable ({url}) : {exc}. "
            "Verifiez que le conteneur ez365-bw-cli tourne et est deverrouille."
        ) from exc

    payload: dict = {}
    try:
        payload = resp.json()
    except Exception:
        pass

    if resp.status_code >= 400 or not payload.get("success", False):
        message = payload.get("message") or resp.text[:300] or f"HTTP {resp.status_code}"
        raise VaultError(f"Bitwarden : {message}")
    return payload.get("data")


def _unwrap_list(data: Any) -> list[dict]:
    if isinstance(data, dict) and data.get("object") == "list":
        return data.get("data", []) or []
    if isinstance(data, list):
        return data
    return []


# ---------------------------------------------------------------------------
async def status() -> dict:
    """Etat du coffre : unlocked / locked / unauthenticated."""
    data = await _call("GET", "/status")
    template = (data or {}).get("template", data) or {}
    return {
        "status": template.get("status", "unknown"),
        "serverUrl": template.get("serverUrl"),
        "userEmail": template.get("userEmail"),
        "lastSync": template.get("lastSync"),
    }


async def is_ready() -> tuple[bool, str]:
    settings = get_settings()
    if not settings.vault_enabled:
        return False, "Coffre desactive"
    try:
        info = await status()
    except VaultError as exc:
        return False, str(exc)
    if info["status"] != "unlocked":
        return False, f"Coffre « {info['status']} » : redemarrez le conteneur ez365-bw-cli."
    return True, f"Connecte a {info.get('serverUrl') or 'Vaultwarden'}"


async def sync() -> None:
    await _call("POST", "/sync")


async def organizations() -> list[dict]:
    data = await _call("GET", "/list/object/organizations")
    return [
        {"id": o.get("id"), "name": o.get("name")}
        for o in _unwrap_list(data)
    ]


async def collections(organization_id: str | None = None) -> list[dict]:
    params = {"organizationid": organization_id} if organization_id else None
    path = "/list/object/org-collections" if organization_id else "/list/object/collections"
    data = await _call("GET", path, params=params)
    return [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "organizationId": c.get("organizationId"),
        }
        for c in _unwrap_list(data)
    ]


async def folders() -> list[dict]:
    data = await _call("GET", "/list/object/folders")
    return [{"id": f.get("id"), "name": f.get("name")} for f in _unwrap_list(data)]


async def search_items(term: str) -> list[dict]:
    data = await _call("GET", "/list/object/items", params={"search": term})
    return _unwrap_list(data)


async def create_login(
    name: str,
    username: str,
    password: str,
    *,
    uri: str = "https://login.microsoftonline.com",
    notes: str = "",
    organization_id: str | None = None,
    collection_ids: list[str] | None = None,
    folder_id: str | None = None,
    fields: list[dict] | None = None,
) -> dict:
    """Cree un identifiant, puis le deplace dans l'organisation si demande."""
    item = {
        "type": TYPE_LOGIN,
        "name": name,
        "notes": notes or None,
        "favorite": False,
        "reprompt": 0,
        "folderId": folder_id,
        "organizationId": organization_id,
        "collectionIds": collection_ids or None,
        "fields": fields or [],
        "login": {
            "username": username,
            "password": password,
            "totp": None,
            "uris": [{"match": None, "uri": uri}] if uri else [],
        },
    }
    created = await _call("POST", "/object/item", json=item)
    item_id = (created or {}).get("id")

    # Selon la version du CLI, organizationId peut etre ignore a la creation :
    # on force alors le rattachement via /move.
    if organization_id and collection_ids and created is not None:
        if not created.get("organizationId"):
            try:
                await _call(
                    "POST",
                    f"/move/{item_id}/{organization_id}",
                    json=collection_ids,
                )
                created["organizationId"] = organization_id
                created["collectionIds"] = collection_ids
            except VaultError as exc:
                log.warning("Rattachement a l'organisation impossible pour %s : %s", name, exc)
                raise VaultError(
                    f"Identifiant « {name} » cree dans le coffre personnel mais non "
                    f"deplace vers l'organisation : {exc}"
                ) from exc
    return created or {}
