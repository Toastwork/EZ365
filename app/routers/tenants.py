"""Tableau de bord, consentement administrateur et fiche tenant."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .. import db, diagnostics, jobs
from ..config import get_settings
from ..msgraph import certificate, oauth, sharepoint
from ..msgraph.client import GraphClient, GraphError
from ..security import Operator, current_operator
from ..templating import flash, render
from ..vault import bitwarden

log = logging.getLogger(__name__)
router = APIRouter()


def get_tenant(tenant_id: str) -> dict:
    row = db.query_one("SELECT * FROM tenants WHERE id = ?", (tenant_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Tenant inconnu")
    return dict(row)


ENTRA_APP_BLADE = (
    "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/"
    "ApplicationMenuBlade/~/{section}/appId/{app_id}"
)


def azure_app_links(app_id: str) -> dict:
    """Liens directs vers l'inscription de l'application EZ365.

    L'inscription se cache facilement : onglet « Applications detenues » par
    defaut, ou mauvais repertoire. Ces liens y menent sans chercher.
    """
    if not app_id:
        return {}
    return {
        section: ENTRA_APP_BLADE.format(section=section, app_id=app_id)
        for section in ("Overview", "Credentials", "CallAnAPI")
    }


def merge_sites(found: list[dict], remembered: list[dict]) -> list[dict]:
    """Sites lus sur le tenant + sites connus d'EZ365, sans doublon.

    Un site tout juste cree peut manquer a l'enumeration Graph pendant
    quelques minutes : ceux qu'EZ365 a crees ou retrouves par leur adresse
    sont ajoutes, et marques comme tels.
    """
    merged: dict[str, dict] = {}
    for site in found:
        key = sharepoint.site_key(site.get("id", ""))
        if key:
            merged[key] = {**site, "pending_index": False}
    for site in remembered:
        key = sharepoint.site_key(site.get("id", ""))
        if key and key not in merged:
            merged[key] = {**site, "pending_index": True}
    return sorted(
        merged.values(),
        key=lambda s: (s.get("displayName") or s.get("name") or "").casefold(),
    )


@router.get("/")
async def dashboard(request: Request, operator: Operator = Depends(current_operator)):
    tenants = [dict(r) for r in db.query(
        "SELECT * FROM tenants ORDER BY display_name COLLATE NOCASE"
    )]
    vault_ready, vault_message = await bitwarden.is_ready()
    return render(
        request,
        "dashboard.html",
        {
            "tenants": tenants,
            "jobs": jobs.recent_jobs(limit=12),
            "vault_ready": vault_ready,
            "vault_message": vault_message,
        },
    )


# ---------------------------------------------------------------------------
# Consentement administrateur
# ---------------------------------------------------------------------------
@router.get("/tenants/connect")
async def connect(request: Request, operator: Operator = Depends(current_operator)):
    oauth.purge_stale_states()
    url = oauth.build_consent_url(operator.username)
    db.audit(operator.username, "tenant.consent.demarre")
    return RedirectResponse(url, status_code=303)


@router.get("/tenants/{tenant_id}/reconsent")
async def reconsent(request: Request, tenant_id: str, operator: Operator = Depends(current_operator)):
    """Renouvelle le consentement d'un client, par exemple apres l'ajout
    d'une permission a l'application."""
    get_tenant(tenant_id)
    oauth.purge_stale_states()
    db.audit(operator.username, "tenant.consent.renouvele", target=tenant_id)
    return RedirectResponse(oauth.build_consent_url(operator.username, tenant_id), status_code=303)


@router.get("/tenants/connect/link")
async def connect_link(request: Request, operator: Operator = Depends(current_operator)):
    """Genere un lien a transmettre a l'administrateur du client."""
    oauth.purge_stale_states()
    return JSONResponse({"url": oauth.build_consent_url(operator.username)})


@router.get("/ms/callback")
async def ms_callback(
    request: Request,
    state: str = Query(""),
    tenant: str = Query(""),
    admin_consent: str = Query(""),
    error: str = Query(""),
    error_description: str = Query(""),
):
    """Retour d'Entra ID apres consentement.

    Volontairement accessible sans session EZ365 : c'est l'administrateur du
    client qui atterrit ici. La securite repose sur le `state` a usage unique.
    """
    if error:
        log.warning("Consentement refuse : %s — %s", error, error_description)
        return render(
            request,
            "consent_result.html",
            {"ok": False, "title": "Consentement non accorde",
             "message": error_description or error},
            status_code=400,
        )
    try:
        actor = oauth.consume_state(state)
    except oauth.ConsentError as exc:
        return render(
            request,
            "consent_result.html",
            {"ok": False, "title": "Lien invalide", "message": str(exc)},
            status_code=400,
        )

    if not tenant:
        return render(
            request,
            "consent_result.html",
            {"ok": False, "title": "Tenant absent",
             "message": "Microsoft n'a pas renvoye d'identifiant de tenant."},
            status_code=400,
        )

    display_name, default_domain, status_label = tenant, "", "ok"
    try:
        async with GraphClient(tenant) as graph:
            org = await graph.organization()
            display_name = org.get("displayName") or tenant
            default_domain = await graph.default_domain()
    except Exception as exc:  # noqa: BLE001
        # Le consentement peut mettre quelques secondes a se propager.
        status_label = "a verifier"
        log.warning("Lecture du tenant %s impossible juste apres consentement : %s", tenant, exc)

    oauth.invalidate(tenant)
    existing = db.query_one("SELECT id FROM tenants WHERE id = ?", (tenant,))
    if existing:
        db.execute(
            "UPDATE tenants SET display_name = ?, default_domain = ?, status = ?,"
            " last_checked_at = ? WHERE id = ?",
            (display_name, default_domain, status_label, db.now(), tenant),
        )
    else:
        db.execute(
            "INSERT INTO tenants(id, display_name, default_domain, consented_by,"
            " consented_at, last_checked_at, status) VALUES (?,?,?,?,?,?,?)",
            (tenant, display_name, default_domain, actor, db.now(), db.now(), status_label),
        )
    db.audit(actor, "tenant.consent.accorde", target=tenant, detail=display_name)
    log.info("Consentement accorde sur %s (%s)", display_name, tenant)

    return render(
        request,
        "consent_result.html",
        {
            "ok": True,
            "title": "Consentement enregistre",
            "message": f"Le tenant « {display_name} » est desormais connecte a EZ365.",
            "tenant_id": tenant,
        },
    )


# ---------------------------------------------------------------------------
# Fiche tenant
# ---------------------------------------------------------------------------
@router.get("/tenants/{tenant_id}")
async def tenant_detail(
    request: Request, tenant_id: str, operator: Operator = Depends(current_operator)
):
    tenant = get_tenant(tenant_id)
    skus: list[dict] = []
    sites: list[dict] = []
    domains: list[str] = []
    graph_error = ""
    try:
        async with GraphClient(tenant_id) as graph:
            skus = [s for s in await graph.subscribed_skus() if s["appliesTo"] == "User"]
            sites = await graph.list_all_sites()
            # Domaines verifies seulement : les autres refusent la creation
            # d'un compte. Le domaine par defaut du tenant vient en tete.
            raw_domains = await graph.domains()
            verified = [d for d in raw_domains if d.get("isVerified")]
            verified.sort(key=lambda d: (not d.get("isDefault"), d.get("id", "")))
            domains = [d["id"] for d in verified if d.get("id")]
    except (GraphError, oauth.ConsentError) as exc:
        graph_error = getattr(exc, "friendly", str(exc))
        log.warning("Lecture du tenant %s impossible : %s", tenant_id, exc)

    try:
        sp_access = await asyncio.wait_for(diagnostics.sharepoint_access(tenant_id), 15)
    except asyncio.TimeoutError:
        sp_access = {"state": "error", "message": "pas de reponse de Microsoft en 15 s"}

    vault_ready, vault_message = await bitwarden.is_ready()
    orgs, collections = [], []
    if vault_ready:
        try:
            orgs = await bitwarden.organizations()
            collections = await bitwarden.collections(tenant.get("vault_org_id") or None)
        except bitwarden.VaultError as exc:
            vault_ready, vault_message = False, str(exc)

    return render(
        request,
        "tenant.html",
        {
            "tenant": tenant,
            "skus": skus,
            "domains": domains or ([tenant["default_domain"]] if tenant.get("default_domain") else []),
            "sites": merge_sites(sites, db.remembered_sites(tenant_id)),
            "graph_error": graph_error,
            "sp_access": sp_access,
            "vault_ready": vault_ready,
            "vault_message": vault_message,
            "vault_orgs": orgs,
            "vault_collections": collections,
            "jobs": jobs.recent_jobs(tenant_id, limit=10),
        },
    )


@router.post("/tenants/{tenant_id}/refresh")
async def tenant_refresh(
    request: Request, tenant_id: str, operator: Operator = Depends(current_operator)
):
    get_tenant(tenant_id)
    oauth.invalidate(tenant_id)
    try:
        async with GraphClient(tenant_id) as graph:
            org = await graph.organization()
            domain = await graph.default_domain()
        db.execute(
            "UPDATE tenants SET display_name = ?, default_domain = ?, status = 'ok',"
            " last_checked_at = ? WHERE id = ?",
            (org.get("displayName") or tenant_id, domain, db.now(), tenant_id),
        )
        flash(request, "Connexion au tenant verifiee.", "success")
    except Exception as exc:  # noqa: BLE001
        db.execute(
            "UPDATE tenants SET status = 'erreur', last_checked_at = ? WHERE id = ?",
            (db.now(), tenant_id),
        )
        flash(request, f"Verification impossible : {exc}", "error")
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/vault")
async def tenant_vault_settings(
    request: Request,
    tenant_id: str,
    vault_org_id: str = Form(""),
    vault_collection_id: str = Form(""),
    operator: Operator = Depends(current_operator),
):
    get_tenant(tenant_id)
    db.execute(
        "UPDATE tenants SET vault_org_id = ?, vault_collection_id = ? WHERE id = ?",
        (vault_org_id or None, vault_collection_id or None, tenant_id),
    )
    db.audit(operator.username, "tenant.coffre.configure", target=tenant_id)
    flash(request, "Destination Bitwarden enregistree.", "success")
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/forget")
async def tenant_forget(
    request: Request, tenant_id: str, operator: Operator = Depends(current_operator)
):
    tenant = get_tenant(tenant_id)
    db.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
    oauth.invalidate(tenant_id)
    db.audit(operator.username, "tenant.retire", target=tenant_id, detail=tenant["display_name"])
    flash(
        request,
        "Tenant retire d'EZ365. Le consentement reste actif cote Microsoft : "
        "revoquez-le dans Entra ID (Applications d'entreprise) si necessaire.",
        "info",
    )
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Diagnostic OneDrive
# ---------------------------------------------------------------------------
@router.get("/tenants/{tenant_id}/diagnostic")
async def diagnostic_page(
    request: Request, tenant_id: str, operator: Operator = Depends(current_operator)
):
    return render(request, "diagnostic.html", {"tenant": get_tenant(tenant_id)})


@router.post("/tenants/{tenant_id}/diagnostic")
async def diagnostic_run(
    request: Request,
    tenant_id: str,
    upn: str = Form(...),
    operator: Operator = Depends(current_operator),
):
    tenant = get_tenant(tenant_id)
    steps = await diagnostics.onedrive_diagnostic(tenant_id, upn)
    db.audit(operator.username, "diagnostic.onedrive", target=tenant_id, detail=upn)
    return render(
        request,
        "diagnostic.html",
        {
            "tenant": tenant,
            "upn": upn,
            "steps": steps,
            "report": diagnostics.as_text(tenant_id, upn, steps),
        },
    )


# Certificat SharePoint
# ---------------------------------------------------------------------------
@router.get("/settings/certificate")
async def certificate_page(request: Request, operator: Operator = Depends(current_operator)):
    cert, error = None, ""
    try:
        cert = certificate.load(create=True)
    except certificate.CertificateError as exc:
        error = str(exc)

    tenants = [dict(r) for r in db.query(
        "SELECT id, display_name FROM tenants ORDER BY display_name COLLATE NOCASE"
    )]

    async def check(tenant: dict) -> dict:
        try:
            state = await asyncio.wait_for(diagnostics.sharepoint_access(tenant["id"]), 20)
        except asyncio.TimeoutError:
            state = {"state": "error", "message": "pas de reponse de Microsoft en 20 s"}
        return {**tenant, **state}

    statuses = await asyncio.gather(*(check(t) for t in tenants)) if cert else []
    return render(
        request,
        "certificate.html",
        {
            "cert": cert,
            "error": error,
            "statuses": statuses,
            "portal": azure_app_links(get_settings().ms_client_id),
        },
    )


@router.get("/settings/certificate.cer")
async def certificate_download(operator: Operator = Depends(current_operator)):
    try:
        cert = certificate.load(create=True)
    except certificate.CertificateError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return Response(
        content=cert.public_der(),
        media_type="application/pkix-cert",
        headers={"Content-Disposition": 'attachment; filename="ez365-sharepoint.cer"'},
    )


@router.post("/settings/certificate/regenerate")
async def certificate_regenerate(
    request: Request, operator: Operator = Depends(current_operator)
):
    try:
        cert = certificate.regenerate()
    except certificate.CertificateError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings/certificate", status_code=303)
    oauth.invalidate_all()
    db.audit(operator.username, "certificat.regenere", detail=cert.thumbprint)
    flash(
        request,
        "Nouveau certificat genere : deposez-le dans Azure, l'ancien ne sert plus.",
        "info",
    )
    return RedirectResponse("/settings/certificate", status_code=303)


@router.get("/api/tenants/{tenant_id}/resolve-site")
async def resolve_site(
    tenant_id: str, url: str = Query(""), operator: Operator = Depends(current_operator)
):
    """Retrouve un site par son adresse, sans passer par l'index de recherche."""
    get_tenant(tenant_id)
    parsed = sharepoint.parse_site_url(url)
    if not parsed:
        return JSONResponse(
            {"error": "Adresse non reconnue : collez l'URL d'un site "
                      "https://<tenant>.sharepoint.com/sites/<nom>."},
            status_code=400,
        )
    hostname, path = parsed
    try:
        async with GraphClient(tenant_id) as graph:
            site = await graph.site_by_path(hostname, path)
    except GraphError as exc:
        return JSONResponse({"error": exc.friendly}, status_code=502)
    if not site:
        return JSONResponse(
            {"error": "Aucun site a cette adresse sur ce tenant."}, status_code=404
        )
    db.remember_site(tenant_id, site, origin="manuel")
    db.audit(operator.username, "site.designe", target=tenant_id, detail=site.get("webUrl"))
    return JSONResponse(
        {
            "id": site["id"],
            "displayName": site.get("displayName") or site.get("name") or path,
            "webUrl": site.get("webUrl"),
        }
    )


@router.get("/api/vault/collections")
async def vault_collections(
    organization_id: str = Query(""), operator: Operator = Depends(current_operator)
):
    try:
        return JSONResponse({"collections": await bitwarden.collections(organization_id or None)})
    except bitwarden.VaultError as exc:
        return JSONResponse({"error": str(exc), "collections": []}, status_code=502)
