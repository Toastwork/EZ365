"""Creation de sites SharePoint et raccourcis OneDrive.

Deux modes de creation :

* `team`          : site d'equipe adosse a un groupe Microsoft 365, cree via
                    Graph (POST /groups). Aucune permission SharePoint REST
                    requise, mais cree aussi un groupe et une boite partagee.
* `communication` : site de communication cree via l'API SharePoint REST
                    (_api/SPSiteManager/create), qui exige un jeton dont
                    l'audience est https://<tenant>.sharepoint.com et la
                    permission applicative Sites.FullControl.All.
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

import httpx

from . import oauth
from .client import GraphClient, GraphError

log = logging.getLogger(__name__)


class SharePointError(Exception):
    pass


def slugify(value: str, max_length: int = 60, fallback: str = "") -> str:
    """Alias ASCII sans espace, pour une URL de site ou une adresse.

    Renvoie `fallback` (vide par defaut) quand il ne reste rien : un repli
    implicite se retrouverait sinon dans les identifiants construits par
    assemblage, par exemple un utilisateur sans nom de famille dont l'UPN
    deviendrait « prenom.site@… ». Les appelants qui veulent un repli le
    demandent explicitement.
    """
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_only).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug[:max_length].strip("-") or fallback).lower()


def mail_nickname(value: str) -> str:
    slug = slugify(value, max_length=54)
    return slug.replace("-", "") or "equipe"


# ---------------------------------------------------------------------------
# Site d'equipe (groupe Microsoft 365)
# ---------------------------------------------------------------------------
async def create_team_site(
    graph: GraphClient,
    display_name: str,
    alias: str,
    description: str = "",
    public: bool = False,
    owner_ids: list[str] | None = None,
) -> dict:
    payload = {
        "displayName": display_name,
        "mailEnabled": True,
        "mailNickname": alias,
        "securityEnabled": False,
        "groupTypes": ["Unified"],
        "description": description or display_name,
        "visibility": "Public" if public else "Private",
    }
    if owner_ids:
        payload["owners@odata.bind"] = [
            f"https://graph.microsoft.com/v1.0/users/{uid}" for uid in owner_ids
        ]
    try:
        group = await graph.create_m365_group(payload)
    except GraphError as exc:
        raise SharePointError(
            f"Creation du groupe Microsoft 365 impossible : {exc.friendly}"
        ) from exc
    return group


async def wait_for_group_site(
    graph: GraphClient, group_id: str, attempts: int = 30, delay: float = 6.0
) -> dict:
    """Le site d'un nouveau groupe met en general 15 a 90 s a exister."""
    last_error: Exception | None = None
    for i in range(attempts):
        try:
            site = await graph.get(f"/groups/{group_id}/sites/root")
            if site and site.get("id"):
                return site
        except GraphError as exc:
            last_error = exc
            if exc.status not in (404, 400, 503):
                raise
        await asyncio.sleep(delay)
    raise SharePointError(
        "Le site du groupe n'est pas encore disponible apres "
        f"{int(attempts * delay)} s. Il finira probablement de se creer seul : "
        f"reverifiez dans quelques minutes. ({last_error})"
    )


# ---------------------------------------------------------------------------
# Site de communication (SharePoint REST)
# ---------------------------------------------------------------------------
async def create_communication_site(
    graph: GraphClient,
    hostname: str,
    display_name: str,
    path: str,
    owner_upn: str,
    description: str = "",
    lcid: int = 1036,
) -> dict:
    """POST _api/SPSiteManager/create. `lcid` 1036 = francais."""
    tenant_scope = f"https://{hostname}/.default"
    token = await oauth.get_app_token(graph.tenant_id, scope=tenant_scope)
    url = f"https://{hostname}/_api/SPSiteManager/create"
    body = {
        "request": {
            "Title": display_name,
            "Url": f"https://{hostname}/sites/{path}",
            "Lcid": lcid,
            "ShareByEmailEnabled": False,
            "Description": description or display_name,
            "WebTemplate": "SITEPAGEPUBLISHING#0",
            "SiteDesignId": "00000000-0000-0000-0000-000000000000",
            "Owner": owner_upn,
        }
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;odata=verbose",
        "Content-Type": "application/json;odata=verbose",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=body, headers=headers)

    if resp.status_code == 403:
        raise SharePointError(
            "SharePoint refuse la creation (403). La permission applicative "
            "Sites.FullControl.All (API SharePoint) est requise pour les sites "
            "de communication ; sinon utilisez le type « site d'equipe »."
        )
    if resp.status_code >= 400:
        raise SharePointError(f"SPSiteManager a repondu {resp.status_code} : {resp.text[:300]}")

    payload = resp.json()
    result = payload.get("d", {}).get("Create", payload)
    status = result.get("SiteStatus")
    # 2 = Ready, 1 = Creating, 3 = Error
    if status == 3:
        raise SharePointError(f"SharePoint a echoue : {result.get('SiteUrl')} (statut 3)")
    return {"webUrl": result.get("SiteUrl"), "siteStatus": status, "raw": result}


async def wait_for_site_by_path(
    graph: GraphClient, hostname: str, server_relative: str,
    attempts: int = 30, delay: float = 6.0,
) -> dict:
    for _ in range(attempts):
        site = await graph.site_by_path(hostname, server_relative)
        if site and site.get("id"):
            return site
        await asyncio.sleep(delay)
    raise SharePointError(
        f"Site {server_relative} toujours introuvable apres {int(attempts * delay)} s."
    )


# ---------------------------------------------------------------------------
# Raccourcis OneDrive
# ---------------------------------------------------------------------------
async def add_shortcut(
    graph: GraphClient,
    user_drive_id: str,
    source_drive_id: str,
    source_item_id: str,
    name: str,
) -> dict:
    """Ajoute « Ajouter un raccourci a OneDrive » dans la racine du OneDrive.

    Note : cet appel est capricieux en app-only ; on tente les deux formes de
    `remoteItem` acceptees par Graph avant d'abandonner proprement.
    """
    variants = [
        {
            "name": name,
            "remoteItem": {
                "id": source_item_id,
                "parentReference": {"driveId": source_drive_id},
            },
            "@microsoft.graph.conflictBehavior": "rename",
        },
        {
            "name": name,
            "remoteItem": {"id": f"{source_drive_id}!{source_item_id}"},
            "@microsoft.graph.conflictBehavior": "rename",
        },
    ]
    last: GraphError | None = None
    for body in variants:
        try:
            return await graph.post(f"/drives/{user_drive_id}/items/root/children", json=body)
        except GraphError as exc:
            last = exc
            if exc.status not in (400, 404, 501):
                raise
    raise SharePointError(
        "Creation du raccourci refusee par Graph "
        f"({last.status if last else '?'} {last.code if last else ''}). "
        "L'API de raccourci OneDrive n'est pas toujours disponible en app-only : "
        "le site reste accessible, seul le raccourci doit etre ajoute manuellement."
    )


async def existing_shortcut_names(graph: GraphClient, user_drive_id: str) -> set[str]:
    try:
        items = await graph.get_all(f"/drives/{user_drive_id}/items/root/children", limit=500)
    except GraphError:
        return set()
    return {(i.get("name") or "").casefold() for i in items}
