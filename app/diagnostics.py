"""Diagnostic du provisionnement OneDrive pour un compte.

Enchaine les appels qu'effectue un traitement, un par un, et consigne pour
chacun la reponse brute de Microsoft. Le rapport s'affiche dans l'interface
(pour etre copie tel quel) et part dans les logs du conteneur. Les jetons ne
sont jamais ecrits : seules leurs revendications utiles le sont (ressource,
application, roles accordes).
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .msgraph import oauth, sharepoint
from .msgraph.client import GraphClient, GraphError

log = logging.getLogger("ez365.diagnostic")

# Plans de service qui portent SharePoint, donc OneDrive.
SHAREPOINT_PLANS = {"SHAREPOINTSTANDARD", "SHAREPOINTENTERPRISE", "SHAREPOINTWAC",
                    "SHAREPOINT_S_DEVELOPER", "SHAREPOINTSTANDARD_EDU",
                    "SHAREPOINTENTERPRISE_EDU", "ONEDRIVE_BASIC", "ONEDRIVESTANDARD"}


@dataclass
class Step:
    title: str
    ok: bool | None = None          # None = information, sans verdict
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def token_claims(token: str) -> dict:
    """Revendications utiles d'un jeton, sans verifier sa signature."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {"illisible": True}
    return {
        "aud": data.get("aud"),
        "appid": data.get("appid") or data.get("azp"),
        "app_displayname": data.get("app_displayname"),
        "tid": data.get("tid"),
        "roles": data.get("roles", []),
        "idtyp": data.get("idtyp"),
    }


REQUIRED_SHAREPOINT_ROLE = "Sites.FullControl.All"
REQUIRED_GRAPH_ROLES = (
    "User.ReadWrite.All", "Organization.Read.All", "Domain.Read.All",
    "Sites.ReadWrite.All",
)
# l'une ou l'autre forme suffit pour les groupes
GROUP_ROLE_SETS = (("Group.ReadWrite.All",), ("Group.Create", "GroupMember.ReadWrite.All"))
MISPLACED_HINT = (
    "Sites.FullControl.All a ete ajoutee sous « Microsoft Graph » : elle doit "
    "l'etre sous « SharePoint » (Autorisations de l'API → Ajouter une autorisation "
    "→ SharePoint → Autorisations de l'application), puis consentement a renouveler."
)


def missing_graph_roles(roles: list) -> list[str]:
    missing = [r for r in REQUIRED_GRAPH_ROLES if r not in roles]
    if not any(all(r in roles for r in group) for group in GROUP_ROLE_SETS):
        missing += [r for r in GROUP_ROLE_SETS[1] if r not in roles]
    return missing
_hostnames: dict[str, str] = {}


async def sharepoint_access(tenant_id: str) -> dict:
    """Etat de l'acces SharePoint d'un tenant, pour guider l'operateur.

    state : ok | reconsent (permission pas encore acceptee par le client)
            | certificate (certificat pas depose sur l'application Azure)
            | error
    """
    try:
        hostname = _hostnames.get(tenant_id)
        if not hostname:
            async with GraphClient(tenant_id) as graph:
                hostname = await graph.sharepoint_hostname()
            _hostnames[tenant_id] = hostname
        token = await oauth.get_app_token(
            tenant_id, scope=f"https://{sharepoint.admin_host_for(hostname)}/.default"
        )
    except oauth.ConsentError as exc:
        text = str(exc)
        if "Certificats & secrets" in text or "certificat" in text.lower():
            return {"state": "certificate", "message": text}
        if "AADSTS65001" in text or "consent" in text.lower():
            return {"state": "reconsent", "message": text}
        return {"state": "error", "message": text}
    except (GraphError, httpx.HTTPError) as exc:
        return {"state": "error", "message": str(exc)}

    roles = token_claims(token).get("roles") or []
    if REQUIRED_SHAREPOINT_ROLE not in roles:
        # jeton en cache anterieur au consentement : en redemander un
        try:
            token = await oauth.get_app_token(
                tenant_id, scope=f"https://{sharepoint.admin_host_for(hostname)}/.default",
                force_refresh=True,
            )
            roles = token_claims(token).get("roles") or []
        except oauth.ConsentError:
            pass
    if REQUIRED_SHAREPOINT_ROLE in roles:
        return {"state": "ok", "message": "Acces SharePoint operationnel.", "roles": roles}
    try:
        graph_roles = token_claims(await oauth.get_app_token(tenant_id)).get("roles") or []
    except oauth.ConsentError:
        graph_roles = []
    if REQUIRED_SHAREPOINT_ROLE in graph_roles:
        return {"state": "misplaced", "message": MISPLACED_HINT, "roles": roles}
    return {
        "state": "reconsent",
        "message": (
            f"Le jeton SharePoint ne contient pas {REQUIRED_SHAREPOINT_ROLE} "
            f"(permissions recues : {', '.join(roles) or 'aucune'}). "
            "Soit la permission n'est pas declaree sur l'application Azure, soit ce "
            "client ne l'a pas encore acceptee, soit le consentement se propage "
            "encore (quelques minutes)."
        ),
        "roles": roles,
    }


def _graph_error(exc: GraphError) -> dict:
    return {"status": exc.status, "code": exc.code, "message": exc.message,
            "requete": exc.request}


async def onedrive_diagnostic(tenant_id: str, upn: str) -> list[Step]:
    steps: list[Step] = []
    upn = (upn or "").strip()

    # 1. Jeton Graph ------------------------------------------------------------
    step = Step("Jeton Microsoft Graph (secret client)")
    try:
        token = await oauth.get_app_token(tenant_id, force_refresh=True)
        claims = token_claims(token)
        step.ok, step.detail = True, claims
        step.summary = f"{len(claims.get('roles') or [])} permission(s) applicative(s) accordee(s)"
        graph_roles = claims.get("roles") or []
        missing = missing_graph_roles(graph_roles)
        if missing:
            step.ok = False
            step.summary += " ; MANQUANTES : " + ", ".join(missing)
        if REQUIRED_SHAREPOINT_ROLE in graph_roles:
            step.ok = False
            step.summary += " ; " + MISPLACED_HINT
    except oauth.ConsentError as exc:
        step.ok, step.summary = False, str(exc)
    steps.append(step)
    if not step.detail:
        return steps

    async with GraphClient(tenant_id) as graph:
        # 2. Compte ---------------------------------------------------------------
        step = Step(f"Compte {upn}")
        user = None
        try:
            user = await graph.get(
                f"/users/{upn}",
                params={"$select": "id,userPrincipalName,accountEnabled,usageLocation,createdDateTime"},
            )
            step.ok, step.detail = True, user
            step.summary = (
                f"actif={user.get('accountEnabled')} · pays={user.get('usageLocation') or 'aucun'}"
            )
        except GraphError as exc:
            step.ok, step.summary, step.detail = False, exc.friendly, _graph_error(exc)
        steps.append(step)
        if not user:
            return steps

        # 3. Licence portant SharePoint -----------------------------------------------
        step = Step("Licence avec SharePoint / OneDrive")
        try:
            details = await graph.user_licenses(user["id"])
            plans = [
                {"licence": d.get("skuPartNumber"), "plan": p.get("servicePlanName"),
                 "etat": p.get("provisioningStatus")}
                for d in details for p in (d.get("servicePlans") or [])
                if p.get("servicePlanName") in SHAREPOINT_PLANS
            ]
            active = [p for p in plans if p["etat"] == "Success"]
            step.ok = bool(active)
            step.summary = (
                ", ".join(f"{p['licence']}/{p['plan']} ({p['etat']})" for p in plans)
                or "aucune licence ne contient SharePoint : pas de OneDrive possible"
            )
            step.detail = {"licences": [d.get("skuPartNumber") for d in details],
                           "plans_sharepoint": plans}
        except GraphError as exc:
            step.ok, step.summary, step.detail = False, exc.friendly, _graph_error(exc)
        steps.append(step)

        # 4. Lecture du OneDrive par Graph ---------------------------------------------
        step = Step("Lecture du OneDrive (Graph)")
        try:
            drive = await graph.get(f"/users/{user['id']}/drive")
            step.ok = True
            step.summary = f"OneDrive present : {drive.get('webUrl')}"
            step.detail = {"id": drive.get("id"), "webUrl": drive.get("webUrl"),
                           "driveType": drive.get("driveType")}
        except GraphError as exc:
            step.ok = False
            step.summary = f"HTTP {exc.status} {exc.code} — {exc.message}"
            step.detail = _graph_error(exc)
        steps.append(step)

        # 5. Jeton SharePoint administration ----------------------------------------------
        step = Step("Jeton SharePoint administration (certificat)")
        admin_host = ""
        sp_token = None
        try:
            hostname = await graph.sharepoint_hostname()
            admin_host = sharepoint.admin_host_for(hostname)
            sp_token = await oauth.get_app_token(
                tenant_id, scope=f"https://{admin_host}/.default", force_refresh=True
            )
            claims = token_claims(sp_token)
            step.ok, step.detail = True, {"scope": f"https://{admin_host}/.default", **claims}
            roles = claims.get("roles") or []
            step.summary = (
                "jeton delivre par Entra ID ; roles SharePoint : "
                + (", ".join(roles) if roles else "AUCUN (permission SharePoint non consentie)")
            )
            if REQUIRED_SHAREPOINT_ROLE not in roles:
                step.ok = False
                step.summary += " ; " + (
                    MISPLACED_HINT if REQUIRED_SHAREPOINT_ROLE in graph_roles
                    else f"{REQUIRED_SHAREPOINT_ROLE} (API SharePoint) absente du jeton"
                )
        except (oauth.ConsentError, GraphError) as exc:
            step.ok, step.summary = False, str(exc)
        steps.append(step)

        # 6. Demande de creation du OneDrive -----------------------------------------------
        if sp_token:
            step = Step("Demande de creation (CreatePersonalSiteEnqueueBulk)")
            try:
                url, resp = await sharepoint.post_enqueue(graph, [user["userPrincipalName"]])
                step.ok = resp.status_code < 400
                step.detail = {"url": url, **sharepoint.describe_response(resp)}
                step.summary = (
                    f"HTTP {resp.status_code} — demande acceptee" if step.ok
                    else f"HTTP {resp.status_code} — refusee par SharePoint"
                )
            except (oauth.ConsentError, httpx.HTTPError) as exc:
                step.ok, step.summary = False, f"{type(exc).__name__} : {exc}"
            steps.append(step)

    for item in steps:
        log.info("[diagnostic %s] %s — %s — %s", upn, item.title,
                 {True: "OK", False: "ECHEC", None: "info"}[item.ok], item.summary)
        if item.detail:
            log.info("[diagnostic %s]   %s", upn,
                     json.dumps(item.detail, ensure_ascii=False, default=str))
    return steps


def as_text(tenant_id: str, upn: str, steps: list[Step]) -> str:
    """Rapport en texte brut, a copier dans un echange."""
    lines = [
        "EZ365 — diagnostic OneDrive",
        f"date   : {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"tenant : {tenant_id}",
        f"compte : {upn}",
        "",
    ]
    for index, step in enumerate(steps, 1):
        verdict = {True: "OK", False: "ECHEC", None: "info"}[step.ok]
        lines.append(f"{index}. [{verdict}] {step.title}")
        if step.summary:
            lines.append(f"   {step.summary}")
        if step.detail:
            for row in json.dumps(step.detail, ensure_ascii=False, indent=2, default=str).splitlines():
                lines.append(f"   {row}")
        lines.append("")
    return "\n".join(lines)
