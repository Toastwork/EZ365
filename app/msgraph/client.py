"""Client Microsoft Graph app-only : pagination, backoff 429/503, erreurs lisibles."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from . import oauth

log = logging.getLogger(__name__)

GRAPH_V1 = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"

_RETRYABLE = {429, 500, 502, 503, 504}


class GraphError(Exception):
    def __init__(self, status: int, code: str, message: str, request: str = ""):
        self.status = status
        self.code = code
        self.message = message
        self.request = request
        super().__init__(f"[{status} {code}] {message}")

    @property
    def friendly(self) -> str:
        mapping = {
            "Request_ResourceNotFound": "Ressource introuvable sur le tenant.",
            "Authorization_RequestDenied": (
                "Permission refusee : l'application n'a pas le droit Graph necessaire. "
                "Verifiez le consentement administrateur."
            ),
            "Directory_QuotaExceeded": "Quota d'objets du tenant atteint.",
        }
        return mapping.get(self.code, self.message)


class GraphClient:
    """Un client par tenant, a utiliser en context manager async."""

    def __init__(self, tenant_id: str, timeout: float = 60.0):
        self.tenant_id = tenant_id
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "GraphClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def close(self) -> None:
        await self._client.aclose()

    # -- bas niveau --------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        beta: bool = False,
        max_retries: int = 5,
    ) -> Any:
        base = GRAPH_BETA if beta else GRAPH_V1
        url = path if path.startswith("http") else f"{base}{path}"
        attempt = 0
        refreshed = False
        while True:
            token = await oauth.get_app_token(self.tenant_id)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            resp = await self._client.request(
                method, url, json=json, params=params, headers=headers
            )

            if resp.status_code in _RETRYABLE and attempt < max_retries:
                delay = _retry_after(resp, attempt)
                log.warning(
                    "Graph %s %s -> %s, nouvelle tentative dans %.1fs",
                    method, url, resp.status_code, delay,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if resp.status_code == 401 and not refreshed:
                oauth.invalidate(self.tenant_id)
                refreshed = True
                continue

            if resp.status_code >= 400:
                raise _to_error(resp, f"{method} {url}")

            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    async def get(self, path: str, **kw) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, json: Any = None, **kw) -> Any:
        return await self.request("POST", path, json=json, **kw)

    async def patch(self, path: str, json: Any = None, **kw) -> Any:
        return await self.request("PATCH", path, json=json, **kw)

    async def delete(self, path: str, **kw) -> Any:
        return await self.request("DELETE", path, **kw)

    async def get_all(
        self, path: str, *, params: dict | None = None, limit: int = 2000
    ) -> list[dict]:
        """Suit @odata.nextLink jusqu'a epuisement (ou `limit`)."""
        items: list[dict] = []
        page = await self.get(path, params=params)
        while page:
            items.extend(page.get("value", []))
            nxt = page.get("@odata.nextLink")
            if not nxt or len(items) >= limit:
                break
            page = await self.get(nxt)
        return items[:limit]

    # -- informations tenant ----------------------------------------------
    async def organization(self) -> dict:
        data = await self.get("/organization")
        values = data.get("value", []) if data else []
        return values[0] if values else {}

    async def domains(self) -> list[dict]:
        return await self.get_all("/domains")

    async def default_domain(self) -> str:
        for dom in await self.domains():
            if dom.get("isDefault"):
                return dom.get("id", "")
        return ""

    async def sharepoint_hostname(self) -> str:
        """Racine SharePoint du tenant, ex. contoso.sharepoint.com."""
        root = await self.get("/sites/root")
        host = (root or {}).get("siteCollection", {}).get("hostname")
        if host:
            return host
        web_url = (root or {}).get("webUrl", "")
        return web_url.split("//", 1)[-1].split("/", 1)[0]

    # -- licences ----------------------------------------------------------
    async def subscribed_skus(self) -> list[dict]:
        skus = await self.get_all("/subscribedSkus")
        result = []
        for sku in skus:
            units = sku.get("prepaidUnits") or {}
            enabled = units.get("enabled", 0) or 0
            consumed = sku.get("consumedUnits", 0) or 0
            result.append(
                {
                    "skuId": sku.get("skuId"),
                    "skuPartNumber": sku.get("skuPartNumber"),
                    "enabled": enabled,
                    "consumed": consumed,
                    "available": max(enabled - consumed, 0),
                    "appliesTo": sku.get("appliesTo"),
                    "capabilityStatus": sku.get("capabilityStatus"),
                }
            )
        result.sort(key=lambda s: (s["skuPartNumber"] or ""))
        return result

    async def assign_license(self, user_id: str, sku_ids: list[str]) -> Any:
        body = {
            "addLicenses": [{"skuId": sku, "disabledPlans": []} for sku in sku_ids],
            "removeLicenses": [],
        }
        return await self.post(f"/users/{user_id}/assignLicense", json=body)

    async def user_licenses(self, user_id: str) -> list[dict]:
        return await self.get_all(f"/users/{user_id}/licenseDetails")

    # -- utilisateurs ------------------------------------------------------
    async def find_user(self, upn: str) -> dict | None:
        try:
            return await self.get(f"/users/{upn}")
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise

    async def create_user(self, payload: dict) -> dict:
        return await self.post("/users", json=payload)

    async def update_user(self, user_id: str, payload: dict) -> None:
        await self.patch(f"/users/{user_id}", json=payload)

    async def list_users(self, search: str = "", limit: int = 200) -> list[dict]:
        params: dict[str, str] = {
            # assignedLicenses evite un appel /licenseDetails par utilisateur :
            # une seule requete suffit pour toute la liste.
            "$select": (
                "id,displayName,userPrincipalName,mail,accountEnabled,"
                "usageLocation,jobTitle,department,assignedLicenses"
            ),
            "$top": "100",
            "$orderby": "displayName",
        }
        if search:
            safe = search.replace("'", "''")
            params["$filter"] = (
                f"startswith(displayName,'{safe}') or "
                f"startswith(userPrincipalName,'{safe}')"
            )
            params.pop("$orderby", None)
        return await self.get_all("/users", params=params, limit=limit)

    # -- OneDrive ----------------------------------------------------------
    async def user_drive(self, user_id: str) -> dict | None:
        """Un GET sur /drive declenche le provisionnement du OneDrive."""
        try:
            return await self.get(f"/users/{user_id}/drive")
        except GraphError as exc:
            if exc.status in (404, 400, 503):
                return None
            raise

    # -- SharePoint (via Graph) --------------------------------------------
    async def site_by_path(self, hostname: str, server_relative: str) -> dict | None:
        path = server_relative.strip("/")
        try:
            return await self.get(f"/sites/{hostname}:/{path}")
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise

    async def search_sites(self, term: str) -> list[dict]:
        term = term or "*"
        return await self.get_all("/sites", params={"search": term}, limit=100)

    async def site_drives(self, site_id: str) -> list[dict]:
        return await self.get_all(f"/sites/{site_id}/drives")

    async def default_site_drive(self, site_id: str) -> dict | None:
        try:
            return await self.get(f"/sites/{site_id}/drive")
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise

    async def drive_root(self, drive_id: str) -> dict:
        return await self.get(f"/drives/{drive_id}/root")

    async def drive_item(self, drive_id: str, item_path: str) -> dict | None:
        path = (item_path or "").strip("/")
        target = f"/drives/{drive_id}/root" if not path else f"/drives/{drive_id}/root:/{path}"
        try:
            return await self.get(target)
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise

    async def list_child_folders(self, drive_id: str, path: str = "") -> list[dict]:
        """Sous-dossiers directs d'un dossier de bibliotheque (racine si vide)."""
        path = (path or "").strip("/")
        target = (
            f"/drives/{drive_id}/root/children"
            if not path
            else f"/drives/{drive_id}/root:/{path}:/children"
        )
        try:
            items = await self.get_all(
                target,
                params={"$select": "id,name,folder,webUrl", "$top": "200"},
                limit=500,
            )
        except GraphError as exc:
            if exc.status == 404:
                return []
            raise
        return [i for i in items if i.get("folder") is not None]

    async def list_folder_tree(
        self, drive_id: str, path: str = "", depth: int = 2, max_items: int = 300
    ) -> list[dict]:
        """Arborescence aplatie des dossiers, pour alimenter une liste deroulante.

        Bornee en profondeur et en nombre : une bibliotheque de client peut
        contenir des milliers de dossiers, et on ne veut ni saturer l'interface
        ni multiplier les appels Graph.
        """
        collected: list[dict] = []

        async def walk(current: str, level: int) -> None:
            if level > depth or len(collected) >= max_items:
                return
            for folder in await self.list_child_folders(drive_id, current):
                # Verifie en tete de boucle : un `return` place apres l'appel
                # recursif n'arreterait que la branche, pas le parcours parent.
                if len(collected) >= max_items:
                    return
                relative = f"{current}/{folder['name']}".strip("/")
                collected.append(
                    {
                        "name": folder["name"],
                        "path": relative,
                        "level": level,
                        "childCount": (folder.get("folder") or {}).get("childCount", 0),
                        "webUrl": folder.get("webUrl"),
                    }
                )
                await walk(relative, level + 1)

        await walk((path or "").strip("/"), 1)
        collected.sort(key=lambda f: f["path"].casefold())
        return collected

    async def create_m365_group(self, payload: dict) -> dict:
        return await self.post("/groups", json=payload)

    async def add_group_member(self, group_id: str, user_id: str) -> None:
        await self.post(
            f"/groups/{group_id}/members/$ref",
            json={"@odata.id": f"{GRAPH_V1}/directoryObjects/{user_id}"},
        )

    async def add_group_owner(self, group_id: str, user_id: str) -> None:
        await self.post(
            f"/groups/{group_id}/owners/$ref",
            json={"@odata.id": f"{GRAPH_V1}/directoryObjects/{user_id}"},
        )


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), 60.0)
        except ValueError:
            pass
    return min(2 ** attempt, 30.0)


def _to_error(resp: httpx.Response, request: str) -> GraphError:
    code, message = "", resp.text[:400]
    try:
        payload = resp.json().get("error", {})
        code = payload.get("code", "") or ""
        message = payload.get("message", message) or message
    except Exception:
        pass
    return GraphError(resp.status_code, code, message, request)
