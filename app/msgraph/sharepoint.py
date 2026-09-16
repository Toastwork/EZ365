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
from urllib.parse import unquote

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


def site_key(site_id: str) -> str:
    """Cle stable d'un site, quelle que soit la forme de son identifiant."""
    return _site_collection_id(site_id)


def parse_site_url(url: str) -> tuple[str, str] | None:
    """« https://x.sharepoint.com/sites/Nom/... » -> (hote, « sites/Nom »).

    Seuls le site racine et les sites sous /sites/ ou /teams/ sont reconnus ;
    le reste de l'adresse (bibliotheque, page, parametres) est ignore.
    """
    match = re.match(r"^\s*https?://([^/\s]+)(/[^?#\s]*)?", url or "", re.IGNORECASE)
    if not match:
        return None
    host = match.group(1).lower()
    if not re.fullmatch(r"[a-z0-9-]+\.sharepoint\.com", host):
        return None
    path = match.group(2) or ""
    site = re.match(r"^/(sites|teams)/([^/]+)", path, re.IGNORECASE)
    if site:
        return host, f"{site.group(1).lower()}/{unquote(site.group(2))}"
    return host, ""


def _site_collection_id(site_id: str) -> str:
    """Identifiant de collection d'un id Graph « hote,collection,web »."""
    parts = (site_id or "").split(",")
    return (parts[1] if len(parts) > 1 else site_id or "").lower()


async def find_site_group(graph: GraphClient, site: dict) -> str | None:
    """Groupe Microsoft 365 qui porte un site d'equipe, s'il y en a un.

    Graph n'expose pas ce lien depuis le site. Un site d'equipe vit sous
    /sites/<alias> ou <alias> est le mailNickname de son groupe : on cherche le
    groupe par cet alias, puis on confirme en comparant son site racine au
    site vise. Un site de communication n'a pas de groupe : on renvoie None.
    """
    match = re.search(r"/(?:sites|teams)/([^/?#]+)", site.get("webUrl") or "")
    if not match:
        return None
    alias = unquote(match.group(1)).replace("'", "''")
    try:
        groups = await graph.get_all(
            "/groups",
            params={
                "$filter": f"mailNickname eq '{alias}'",
                "$select": "id,displayName,groupTypes",
            },
            limit=10,
        )
    except GraphError as exc:
        log.warning("Recherche du groupe du site impossible : %s", exc)
        return None

    wanted = _site_collection_id(site.get("id", ""))
    for group in groups:
        if "Unified" not in (group.get("groupTypes") or []):
            continue
        try:
            root = await graph.get(f"/groups/{group['id']}/sites/root")
        except GraphError:
            continue
        if root and _site_collection_id(root.get("id", "")) == wanted:
            return group["id"]
    return None


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


ENQUEUE_PATH = (
    "/_api/SP.UserProfiles.ProfileLoader.GetProfileLoader/CreatePersonalSiteEnqueueBulk"
)


def admin_host_for(hostname: str) -> str:
    """contoso.sharepoint.com -> contoso-admin.sharepoint.com"""
    return hostname.replace(".sharepoint.com", "-admin.sharepoint.com", 1)


def describe_response(resp: httpx.Response) -> dict:
    """Ce qu'il faut pour comprendre un refus SharePoint, sans le jeton."""
    return {
        "status": resp.status_code,
        "x-ms-diagnostics": resp.headers.get("x-ms-diagnostics", ""),
        "request-id": resp.headers.get("request-id")
        or resp.headers.get("sprequestguid", ""),
        "www-authenticate": resp.headers.get("www-authenticate", ""),
        "body": resp.text[:800],
    }


async def post_enqueue(graph: GraphClient, emails: list[str]) -> tuple[str, httpx.Response]:
    """Un appel CreatePersonalSiteEnqueueBulk ; renvoie (url, reponse brute)."""
    hostname = await graph.sharepoint_hostname()
    admin_host = admin_host_for(hostname)
    token = await oauth.get_app_token(graph.tenant_id, scope=f"https://{admin_host}/.default")
    url = f"https://{admin_host}{ENQUEUE_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;odata=nometadata",
        "Content-Type": "application/json;odata=nometadata",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json={"emailIDs": emails}, headers=headers)
    return url, resp


async def enqueue_personal_sites(
    graph: GraphClient, emails: list[str]
) -> tuple[bool, str, dict]:
    """Demande a SharePoint de creer les OneDrive (equivalent de Request-SPOPersonalSite).

    Renvoie (succes, message, detail brut de la reponse). Le detail part dans le
    journal du traitement pour qu'un refus puisse etre analyse tel quel.
    """
    if not emails:
        return True, "", {}
    # La route accepte 200 adresses par appel.
    for start in range(0, len(emails), 200):
        url, resp = await post_enqueue(graph, emails[start:start + 200])
        if resp.status_code >= 400:
            detail = {"url": url, **describe_response(resp)}
            diag = detail["x-ms-diagnostics"]
            message = f"HTTP {resp.status_code}" + (f" — {diag}" if diag else "")
            return False, message, detail
    return True, "", {}


async def existing_shortcut_names(graph: GraphClient, user_drive_id: str) -> set[str]:
    try:
        items = await graph.get_all(f"/drives/{user_drive_id}/items/root/children", limit=500)
    except GraphError:
        return set()
    return {(i.get("name") or "").casefold() for i in items}
