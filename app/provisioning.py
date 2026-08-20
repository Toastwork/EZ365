"""Orchestration du provisionnement d'un client Microsoft 365.

Deroule, dans cet ordre :
  1. site SharePoint (cree ou existant) ;
  2. comptes utilisateurs (creation, usageLocation, licences, appartenance) ;
  3. declenchement puis attente des OneDrive (une licence est necessaire) ;
  4. raccourcis vers la bibliotheque du site dans chaque OneDrive ;
  5. depot des identifiants dans Bitwarden.

L'ordre compte : sans licence pas de OneDrive, et sans OneDrive pas de
raccourci. Les etapes 3 et 4 sont donc lancees pour tous les utilisateurs a la
fois puis attendues, plutot que sequentiellement compte par compte.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import db, passwords
from .jobs import JobContext
from .msgraph import sharepoint
from .msgraph.client import GraphClient, GraphError
from .vault import bitwarden

log = logging.getLogger(__name__)

ONEDRIVE_ATTEMPTS = 20
ONEDRIVE_DELAY = 15.0


# ---------------------------------------------------------------------------
# Normalisation des donnees du formulaire
# ---------------------------------------------------------------------------
def normalize_user(raw: dict, domain: str, default_usage_location: str) -> dict:
    first = (raw.get("first_name") or "").strip()
    last = (raw.get("last_name") or "").strip()
    display = (raw.get("display_name") or f"{first} {last}").strip()

    upn = (raw.get("upn") or "").strip().lower()
    if not upn:
        local = (raw.get("alias") or "").strip().lower()
        if not local:
            local = ".".join(p for p in (sharepoint.slugify(first), sharepoint.slugify(last)) if p)
        upn = f"{local}@{domain}"
    elif "@" not in upn:
        upn = f"{upn}@{domain}"

    return {
        "first_name": first,
        "last_name": last,
        "display_name": display or upn.split("@")[0],
        "upn": upn,
        "job_title": (raw.get("job_title") or "").strip(),
        "department": (raw.get("department") or "").strip(),
        "usage_location": (raw.get("usage_location") or default_usage_location or "FR").upper()[:2],
        "password": (raw.get("password") or "").strip(),
        "force_change": bool(raw.get("force_change", True)),
        # Dossier de la bibliotheque a raccourcir pour cette personne ;
        # vide = dossier par defaut du traitement.
        "shortcut_folder": (raw.get("shortcut_folder") or "").strip().strip("/"),
        # Compte choisi dans la liste des utilisateurs deja presents sur le
        # tenant : on ne doit jamais le creer, seulement l'utiliser.
        "existing_only": bool(raw.get("existing_only")),
        # Licences propres a cette personne, deja resolues par le routeur
        # (choix de la ligne, sinon licence par defaut du traitement).
        "sku_ids": list(raw.get("sku_ids") or []),
        "sku_names": list(raw.get("sku_names") or []),
    }


# ---------------------------------------------------------------------------
# Etape 1 : site SharePoint
# ---------------------------------------------------------------------------
async def ensure_site(ctx: JobContext, graph: GraphClient, spec: dict) -> dict | None:
    mode = spec.get("mode", "none")
    if mode == "none":
        ctx.info("sharepoint", "Aucun site demande, etape ignoree.")
        return None

    hostname = await graph.sharepoint_hostname()

    if mode == "existing":
        site_id = spec.get("site_id")
        if not site_id:
            raise ValueError("Aucun site existant selectionne.")
        site = await graph.get(f"/sites/{site_id}")
        ctx.success("sharepoint", f"Site existant utilise : {site.get('webUrl')}")
        return {"id": site["id"], "webUrl": site.get("webUrl"), "groupId": None}

    display_name = (spec.get("display_name") or "").strip()
    if not display_name:
        raise ValueError("Le nom du site est obligatoire.")
    # Ici le repli est legitime : une adresse de site ne peut pas etre vide.
    path = sharepoint.slugify(spec.get("path") or display_name, fallback="site")
    description = spec.get("description", "")

    if mode == "team":
        alias = sharepoint.mail_nickname(spec.get("path") or display_name)
        ctx.info("sharepoint", f"Creation du site d'equipe « {display_name} » (alias {alias})…")
        group = await sharepoint.create_team_site(
            graph, display_name, alias, description, public=bool(spec.get("public"))
        )
        ctx.info("sharepoint", f"Groupe Microsoft 365 cree ({group['id']}), attente du site…")
        site = await sharepoint.wait_for_group_site(graph, group["id"])
        ctx.success("sharepoint", f"Site d'equipe pret : {site.get('webUrl')}")
        return {"id": site["id"], "webUrl": site.get("webUrl"), "groupId": group["id"]}

    if mode == "communication":
        owner = spec.get("owner_upn") or ""
        if not owner:
            raise ValueError(
                "Un proprietaire (UPN existant sur le tenant) est requis pour un site "
                "de communication."
            )
        ctx.info("sharepoint", f"Creation du site de communication /sites/{path}…")
        await sharepoint.create_communication_site(
            graph, hostname, display_name, path, owner, description
        )
        site = await sharepoint.wait_for_site_by_path(graph, hostname, f"sites/{path}")
        ctx.success("sharepoint", f"Site de communication pret : {site.get('webUrl')}")
        return {"id": site["id"], "webUrl": site.get("webUrl"), "groupId": None}

    raise ValueError(f"Mode de site inconnu : {mode}")


async def resolve_shortcut_target(
    ctx: JobContext, graph: GraphClient, site: dict, folder_path: str = ""
) -> dict | None:
    """Repere la bibliotheque (et eventuellement le dossier) a raccourcir."""
    drive = await graph.default_site_drive(site["id"])
    if not drive:
        drives = await graph.site_drives(site["id"])
        drive = drives[0] if drives else None
    if not drive:
        ctx.warn("raccourcis", "Aucune bibliotheque de documents trouvee sur le site.")
        return None

    item = await graph.drive_item(drive["id"], folder_path)
    if not item:
        ctx.warn(
            "raccourcis",
            f"Dossier « {folder_path} » introuvable dans la bibliotheque, "
            "le raccourci pointera sur la racine.",
        )
        item = await graph.drive_root(drive["id"])
    return {"driveId": drive["id"], "itemId": item["id"], "name": drive.get("name", "Documents")}


# ---------------------------------------------------------------------------
# Etape 2 : utilisateurs
# ---------------------------------------------------------------------------
async def create_users(
    ctx: JobContext, graph: GraphClient, users: list[dict], site: dict | None
) -> list[dict]:
    results: list[dict] = []
    for spec in users:
        entry: dict[str, Any] = {
            "upn": spec["upn"],
            "display_name": spec["display_name"],
            "password": spec["password"] or passwords.generate(),
            "created": False,
            "existing": False,
            "licenses": [],
            "onedrive": "en attente",
            "shortcut": "en attente",
            "shortcut_folder": spec.get("shortcut_folder", ""),
            "license_names": list(spec.get("sku_names") or []),
            "vault": "en attente",
            "errors": [],
        }
        try:
            existing = await graph.find_user(spec["upn"])
            if existing:
                entry["id"] = existing["id"]
                entry["existing"] = True
                entry["password"] = ""
                entry["display_name"] = existing.get("displayName") or entry["display_name"]
                if spec["existing_only"]:
                    ctx.info("utilisateurs", f"Compte existant retenu : {spec['upn']}")
                else:
                    ctx.warn(
                        "utilisateurs",
                        f"{spec['upn']} existe deja : compte reutilise, mot de passe inchange.",
                    )
                if not existing.get("usageLocation"):
                    await graph.update_user(
                        existing["id"], {"usageLocation": spec["usage_location"]}
                    )
            elif spec["existing_only"]:
                raise ValueError(
                    "compte introuvable sur le tenant : il figurait pourtant dans la "
                    "liste des utilisateurs existants (a-t-il ete supprime depuis ?)"
                )
            else:
                payload = {
                    "accountEnabled": True,
                    "displayName": spec["display_name"],
                    "mailNickname": sharepoint.mail_nickname(spec["upn"].split("@")[0]),
                    "userPrincipalName": spec["upn"],
                    "usageLocation": spec["usage_location"],
                    "passwordProfile": {
                        "forceChangePasswordNextSignIn": spec["force_change"],
                        "password": entry["password"],
                    },
                }
                if spec["first_name"]:
                    payload["givenName"] = spec["first_name"]
                if spec["last_name"]:
                    payload["surname"] = spec["last_name"]
                if spec["job_title"]:
                    payload["jobTitle"] = spec["job_title"]
                if spec["department"]:
                    payload["department"] = spec["department"]

                created = await graph.create_user(payload)
                entry["id"] = created["id"]
                entry["created"] = True
                ctx.success("utilisateurs", f"Compte cree : {spec['upn']}")

            # -- licences --------------------------------------------------
            sku_ids = spec.get("sku_ids") or []
            if sku_ids:
                try:
                    await graph.assign_license(entry["id"], sku_ids)
                    entry["licenses"] = sku_ids
                    ctx.success(
                        "licences",
                        f"{', '.join(spec.get('sku_names') or sku_ids)} attribuee(s) "
                        f"a {spec['upn']}",
                    )
                except GraphError as exc:
                    entry["errors"].append(f"licence : {exc.friendly}")
                    ctx.error("licences", f"Licence refusee pour {spec['upn']} : {exc.friendly}")

            # -- appartenance au groupe du site d'equipe -------------------
            if site and site.get("groupId"):
                try:
                    await graph.add_group_member(site["groupId"], entry["id"])
                    ctx.info("sharepoint", f"{spec['upn']} ajoute au groupe du site.")
                except GraphError as exc:
                    if exc.status not in (400,):  # 400 = deja membre
                        entry["errors"].append(f"groupe : {exc.friendly}")
                        ctx.warn("sharepoint", f"Ajout au groupe impossible : {exc.friendly}")

        except GraphError as exc:
            entry["errors"].append(exc.friendly)
            ctx.error("utilisateurs", f"Echec sur {spec['upn']} : {exc.friendly}")
        except Exception as exc:  # noqa: BLE001
            entry["errors"].append(str(exc))
            ctx.error("utilisateurs", f"Echec sur {spec['upn']} : {exc}")

        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Etape 3 : OneDrive
# ---------------------------------------------------------------------------
async def provision_onedrives(ctx: JobContext, graph: GraphClient, results: list[dict]) -> None:
    targets = [r for r in results if r.get("id")]
    if not targets:
        return

    ctx.info(
        "onedrive",
        f"Declenchement du provisionnement OneDrive pour {len(targets)} compte(s). "
        "Microsoft peut mettre plusieurs minutes.",
    )
    for entry in targets:
        try:
            await graph.user_drive(entry["id"])
        except GraphError as exc:
            log.debug("Amorce OneDrive %s : %s", entry["upn"], exc)

    pending = {e["upn"]: e for e in targets}
    for attempt in range(1, ONEDRIVE_ATTEMPTS + 1):
        if not pending:
            break
        await asyncio.sleep(ONEDRIVE_DELAY)
        for upn in list(pending):
            entry = pending[upn]
            try:
                drive = await graph.user_drive(entry["id"])
            except GraphError as exc:
                log.debug("Attente OneDrive %s : %s", upn, exc)
                continue
            if drive and drive.get("id"):
                entry["drive_id"] = drive["id"]
                entry["onedrive"] = "pret"
                ctx.success("onedrive", f"OneDrive pret pour {upn}")
                pending.pop(upn, None)
        if pending and attempt % 4 == 0:
            ctx.info(
                "onedrive",
                f"Toujours en attente pour {len(pending)} compte(s) "
                f"({int(attempt * ONEDRIVE_DELAY)} s ecoulees)…",
            )

    for upn, entry in pending.items():
        entry["onedrive"] = "non provisionne"
        entry["errors"].append("OneDrive non provisionne dans le delai imparti")
        ctx.warn(
            "onedrive",
            f"OneDrive de {upn} pas encore pret. Il se creera seul (souvent a la "
            "premiere connexion) ; le raccourci devra alors etre repasse.",
        )


# ---------------------------------------------------------------------------
# Etape 4 : raccourcis
# ---------------------------------------------------------------------------
async def add_shortcuts(
    ctx: JobContext,
    graph: GraphClient,
    results: list[dict],
    site: dict,
    default_folder: str,
    default_label: str,
) -> None:
    """Chaque utilisateur peut viser un dossier different de la bibliotheque.

    Les cibles sont resolues une seule fois par dossier : dix utilisateurs
    pointant sur « Comptabilite » ne declenchent qu'un aller-retour Graph.
    """
    targets: dict[str, dict | None] = {}

    async def target_for(folder: str) -> dict | None:
        if folder not in targets:
            targets[folder] = await resolve_shortcut_target(ctx, graph, site, folder)
        return targets[folder]

    for entry in results:
        drive_id = entry.get("drive_id")
        if not drive_id:
            entry["shortcut"] = "impossible (OneDrive absent)"
            continue

        folder = (entry.get("shortcut_folder") or default_folder or "").strip("/")
        target = await target_for(folder)
        if not target:
            entry["shortcut"] = "cible introuvable"
            entry["errors"].append(f"raccourci : dossier « {folder} » introuvable")
            continue

        # Le raccourci prend le nom du dossier vise, sinon celui du site.
        label = folder.rsplit("/", 1)[-1] if folder else default_label
        entry["shortcut_target"] = folder or "(racine)"

        try:
            existing = await sharepoint.existing_shortcut_names(graph, drive_id)
            if label.casefold() in existing:
                entry["shortcut"] = "deja present"
                ctx.info("raccourcis", f"Raccourci « {label} » deja present chez {entry['upn']}")
                continue
            await sharepoint.add_shortcut(
                graph, drive_id, target["driveId"], target["itemId"], label
            )
            entry["shortcut"] = "ajoute"
            ctx.success("raccourcis", f"Raccourci « {label} » ajoute chez {entry['upn']}")
        except (sharepoint.SharePointError, GraphError) as exc:
            entry["shortcut"] = "echec"
            entry["errors"].append(f"raccourci : {exc}")
            ctx.warn("raccourcis", f"Raccourci impossible pour {entry['upn']} : {exc}")


# ---------------------------------------------------------------------------
# Etape 5 : Bitwarden
# ---------------------------------------------------------------------------
async def store_in_vault(
    ctx: JobContext, results: list[dict], tenant: dict, vault_spec: dict
) -> None:
    if not vault_spec.get("enabled"):
        for entry in results:
            entry["vault"] = "desactive"
        ctx.info("bitwarden", "Depot dans le coffre desactive pour ce traitement.")
        return

    ready, message = await bitwarden.is_ready()
    if not ready:
        for entry in results:
            entry["vault"] = "indisponible"
        ctx.error("bitwarden", f"Coffre indisponible : {message}")
        return

    org_id = vault_spec.get("organization_id") or None
    collection_id = vault_spec.get("collection_id") or None
    collection_ids = [collection_id] if collection_id else None
    client_name = tenant.get("display_name") or tenant.get("default_domain") or tenant["id"]

    for entry in results:
        if not entry.get("created"):
            entry["vault"] = "ignore (compte existant)"
            continue
        if not entry.get("password"):
            entry["vault"] = "ignore (pas de mot de passe)"
            continue
        try:
            notes = (
                f"Compte Microsoft 365 cree par EZ365 le {db.now()}.\n"
                f"Client : {client_name}\nTenant : {tenant['id']}\n"
                f"Licences : {', '.join(entry.get('license_names') or []) or 'aucune'}"
            )
            await bitwarden.create_login(
                name=f"M365 — {client_name} — {entry['display_name']}",
                username=entry["upn"],
                password=entry["password"],
                uri="https://portal.office.com",
                notes=notes,
                organization_id=org_id,
                collection_ids=collection_ids,
                fields=[
                    {"name": "Tenant", "value": tenant["id"], "type": 0},
                    {"name": "Cree par", "value": ctx.actor, "type": 0},
                ],
            )
            entry["vault"] = "enregistre"
            entry["password_shown"] = False
            ctx.success("bitwarden", f"Identifiant de {entry['upn']} depose dans le coffre.")
        except bitwarden.VaultError as exc:
            entry["vault"] = "echec"
            entry["password_shown"] = True
            entry["errors"].append(f"coffre : {exc}")
            ctx.error(
                "bitwarden",
                f"Depot impossible pour {entry['upn']} : {exc} "
                "— le mot de passe est affiche dans le recapitulatif, a mettre au "
                "coffre manuellement.",
            )


# ---------------------------------------------------------------------------
# Orchestration complete
# ---------------------------------------------------------------------------
async def run_provisioning(ctx: JobContext, tenant: dict, spec: dict) -> dict:
    site_spec = spec.get("site") or {"mode": "none"}
    user_specs = spec.get("users") or []
    vault_spec = spec.get("vault") or {"enabled": False}
    do_onedrive = bool(spec.get("provision_onedrive", True))
    do_shortcut = bool(spec.get("add_shortcut", True))

    ctx.info(
        "demarrage",
        f"Tenant {tenant.get('display_name') or tenant['id']} — "
        f"{len(user_specs)} utilisateur(s), site : {site_spec.get('mode')}.",
    )

    async with GraphClient(tenant["id"]) as graph:
        site = await ensure_site(ctx, graph, site_spec)

        results = await create_users(ctx, graph, user_specs, site)

        if do_onedrive:
            await provision_onedrives(ctx, graph, results)
        else:
            for entry in results:
                entry["onedrive"] = "non demande"

        if do_shortcut and site:
            default_label = site_spec.get("shortcut_label") or (
                site_spec.get("display_name") or "Documents"
            )
            await add_shortcuts(
                ctx,
                graph,
                results,
                site,
                site_spec.get("shortcut_folder", ""),
                default_label,
            )
        else:
            for entry in results:
                entry["shortcut"] = "non demande"

        await store_in_vault(ctx, results, tenant, vault_spec)

    created = sum(1 for r in results if r["created"])
    has_errors = any(r["errors"] for r in results)

    # Les mots de passe ne sont conserves dans le recapitulatif que si le
    # depot au coffre a echoue : sinon Bitwarden est la seule source.
    for entry in results:
        if not entry.get("password_shown"):
            entry["password"] = ""

    summary = {
        "site": site,
        "users": results,
        "created": created,
        "total": len(results),
        "has_errors": has_errors,
    }
    ctx.info(
        "recapitulatif",
        f"{created} compte(s) cree(s) sur {len(results)}."
        + (" Des erreurs sont a examiner." if has_errors else ""),
    )
    return summary
