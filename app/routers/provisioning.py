"""Formulaire de provisionnement, lancement et suivi des traitements."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import db, jobs, provisioning
from ..msgraph.client import GraphClient, GraphError
from ..security import Operator, current_operator
from ..templating import flash, render
from .tenants import get_tenant

log = logging.getLogger(__name__)
router = APIRouter()

MAX_USERS = 200


def parse_bulk(text: str) -> list[dict]:
    """Une ligne par utilisateur : Prenom;Nom;alias;Fonction;Service.

    Le point-virgule, la virgule et la tabulation sont acceptes comme
    separateurs (un copier-coller d'Excel arrive en tabulations).
    """
    users: list[dict] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("\t", ";", ","):
            if sep in line:
                parts = [p.strip() for p in line.split(sep)]
                break
        else:
            parts = [line]
        parts += [""] * (5 - len(parts))
        users.append(
            {
                "first_name": parts[0],
                "last_name": parts[1],
                "alias": parts[2],
                "job_title": parts[3],
                "department": parts[4],
            }
        )
    return users


@router.post("/tenants/{tenant_id}/provision")
async def start_provisioning(
    request: Request,
    tenant_id: str,
    operator: Operator = Depends(current_operator),
):
    tenant = get_tenant(tenant_id)
    form = await request.form()

    site_mode = form.get("site_mode", "none")
    site_spec = {
        "mode": site_mode,
        "display_name": (form.get("site_display_name") or "").strip(),
        "path": (form.get("site_path") or "").strip(),
        "description": (form.get("site_description") or "").strip(),
        "public": form.get("site_public") == "on",
        "site_id": (form.get("existing_site_id") or "").strip(),
        "owner_upn": (form.get("site_owner_upn") or "").strip(),
        "shortcut_label": (form.get("shortcut_label") or "").strip(),
        "shortcut_folder": (form.get("shortcut_folder") or "").strip(),
    }

    # -- utilisateurs : lignes du tableau + collage en masse -----------------
    first_names = form.getlist("first_name")
    last_names = form.getlist("last_name")
    aliases = form.getlist("alias")
    job_titles = form.getlist("job_title")
    departments = form.getlist("department")

    raw_users: list[dict] = []
    for i in range(len(first_names)):
        first = (first_names[i] or "").strip()
        last = (last_names[i] if i < len(last_names) else "").strip()
        alias = (aliases[i] if i < len(aliases) else "").strip()
        if not (first or last or alias):
            continue
        raw_users.append(
            {
                "first_name": first,
                "last_name": last,
                "alias": alias,
                "job_title": (job_titles[i] if i < len(job_titles) else "").strip(),
                "department": (departments[i] if i < len(departments) else "").strip(),
            }
        )
    raw_users.extend(parse_bulk(form.get("bulk_users", "")))

    if not raw_users:
        flash(request, "Aucun utilisateur a creer : le formulaire est vide.", "error")
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
    if len(raw_users) > MAX_USERS:
        flash(request, f"Limite de {MAX_USERS} utilisateurs par traitement depassee.", "error")
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)

    domain = (form.get("user_domain") or tenant.get("default_domain") or "").strip()
    if not domain:
        flash(request, "Domaine des comptes introuvable : renseignez-le.", "error")
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)

    usage_location = (form.get("usage_location") or "FR").strip().upper()[:2]
    force_change = form.get("force_change") == "on"
    users = []
    for raw in raw_users:
        raw["force_change"] = force_change
        users.append(provisioning.normalize_user(raw, domain, usage_location))

    duplicates = {u["upn"] for u in users if [x["upn"] for x in users].count(u["upn"]) > 1}
    if duplicates:
        flash(request, f"Doublons dans la liste : {', '.join(sorted(duplicates))}", "error")
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)

    # Les cases a cocher portent la valeur "<skuId>|<libelle>" : une case non
    # cochee n'est pas envoyee, ce qui evite d'avoir a apparier deux champs.
    sku_ids, sku_names = [], []
    for value in form.getlist("sku_id"):
        if not value:
            continue
        sku_id, _, label = value.partition("|")
        sku_ids.append(sku_id)
        sku_names.append(label or sku_id)

    vault_spec = {
        "enabled": form.get("vault_enabled") == "on",
        "organization_id": (form.get("vault_org_id") or tenant.get("vault_org_id") or "").strip(),
        "collection_id": (
            form.get("vault_collection_id") or tenant.get("vault_collection_id") or ""
        ).strip(),
    }

    spec = {
        "site": site_spec,
        "users": users,
        "sku_ids": sku_ids,
        "sku_names": sku_names,
        "provision_onedrive": form.get("provision_onedrive") == "on",
        "add_shortcut": form.get("add_shortcut") == "on",
        "vault": vault_spec,
    }

    job_id = jobs.create_job(tenant_id, "provisionnement", operator.username, spec)
    db.audit(
        operator.username,
        "provisionnement.lance",
        target=tenant_id,
        detail={"job": job_id, "utilisateurs": len(users), "site": site_mode},
    )

    async def runner(ctx: jobs.JobContext) -> dict:
        return await provisioning.run_provisioning(ctx, tenant, spec)

    jobs.launch(job_id, tenant_id, operator.username, runner)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/jobs/{job_id}")
async def job_page(request: Request, job_id: str, operator: Operator = Depends(current_operator)):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Traitement inconnu")
    tenant = db.query_one("SELECT * FROM tenants WHERE id = ?", (job["tenant_id"],))
    return render(
        request,
        "job.html",
        {
            "job": job,
            "tenant": dict(tenant) if tenant else None,
            "events": jobs.events(job_id),
            "running": job["status"] in ("pending", "running"),
        },
    )


@router.get("/jobs/{job_id}/events")
async def job_events(
    request: Request,
    job_id: str,
    after: int = Query(0),
    operator: Operator = Depends(current_operator),
):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Traitement inconnu")
    return JSONResponse(
        {
            "status": job["status"],
            "running": job["status"] in ("pending", "running"),
            "events": jobs.events(job_id, after),
        }
    )


@router.get("/jobs/{job_id}/summary")
async def job_summary(
    request: Request, job_id: str, operator: Operator = Depends(current_operator)
):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Traitement inconnu")
    return render(request, "partials/summary.html", {"job": job})


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(
    request: Request, job_id: str, operator: Operator = Depends(current_operator)
):
    if jobs.cancel(job_id):
        db.audit(operator.username, "traitement.annule", target=job_id)
        flash(request, "Traitement interrompu.", "info")
    else:
        flash(request, "Ce traitement n'est plus en cours.", "info")
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/api/tenants/{tenant_id}/users")
async def search_users(
    tenant_id: str, q: str = Query(""), operator: Operator = Depends(current_operator)
):
    get_tenant(tenant_id)
    try:
        async with GraphClient(tenant_id) as graph:
            users = await graph.list_users(q, limit=25)
    except GraphError as exc:
        return JSONResponse({"error": exc.friendly, "users": []}, status_code=502)
    return JSONResponse(
        {
            "users": [
                {
                    "id": u.get("id"),
                    "displayName": u.get("displayName"),
                    "userPrincipalName": u.get("userPrincipalName"),
                }
                for u in users
            ]
        }
    )
