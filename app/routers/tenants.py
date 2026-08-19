"""Tableau de bord, consentement administrateur et fiche tenant."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import db, jobs
from ..msgraph import oauth
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
    graph_error = ""
    try:
        async with GraphClient(tenant_id) as graph:
            skus = [s for s in await graph.subscribed_skus() if s["appliesTo"] == "User"]
            sites = await graph.search_sites("*")
    except (GraphError, oauth.ConsentError) as exc:
        graph_error = getattr(exc, "friendly", str(exc))
        log.warning("Lecture du tenant %s impossible : %s", tenant_id, exc)

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
            "sites": sorted(sites, key=lambda s: (s.get("displayName") or "")),
            "graph_error": graph_error,
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


@router.get("/api/vault/collections")
async def vault_collections(
    organization_id: str = Query(""), operator: Operator = Depends(current_operator)
):
    try:
        return JSONResponse({"collections": await bitwarden.collections(organization_id or None)})
    except bitwarden.VaultError as exc:
        return JSONResponse({"error": str(exc), "collections": []}, status_code=502)
