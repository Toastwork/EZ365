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
# Nom des entrees deposees dans Bitwarden
# ---------------------------------------------------------------------------
ONMICROSOFT = ".onmicrosoft.com"
CLIENT_PLACEHOLDER = "[CLIENT]"


def vault_client_code(domain: str) -> str:
    """Code client tire du domaine : « acskm.fr » -> « ACSKM ».

    Un domaine en .onmicrosoft.com ne nomme pas le client (c'est le domaine
    technique du tenant) : on laisse alors le marqueur [CLIENT] en clair, a
    completer par l'operateur.
    """
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain or domain.endswith(ONMICROSOFT):
        return CLIENT_PLACEHOLDER
    return domain.split(".")[0].upper()


def default_vault_name(domain: str, upn: str) -> str:
    """Nom par defaut d'une entree de coffre : CLIENT-OFFICE-UTILISATEUR."""
    local = (upn or "").split("@")[0].strip().upper()
    return f"{vault_client_code(domain)}-OFFICE-{local}"


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
        # Dossiers a raccourcir pour cette personne. Une chaine vide dans la
        # liste designe la racine de la bibliotheque.
        "shortcut_folders": [
            (f or "").strip().strip("/") for f in (raw.get("shortcut_folders") or [])
        ],
        # Provisionner (et attendre) le OneDrive de cette personne. Demander un
        # raccourci l'implique : il n'y a nulle part ou le poser sinon.
        "provision_onedrive": bool(raw.get("provision_onedrive"))
        or bool(raw.get("shortcut_folders")),
        # Compte choisi dans la liste des utilisateurs deja presents sur le
        # tenant : on ne doit jamais le creer, seulement l'utiliser.
        "existing_only": bool(raw.get("existing_only")),
        # Licences propres a cette personne, deja resolues par le routeur
        # (choix de la ligne, sinon licence par defaut du traitement).
        "sku_ids": list(raw.get("sku_ids") or []),
        "sku_names": list(raw.get("sku_names") or []),
        # Depot au coffre : uniquement pour un compte cree, dont on connait le
        # mot de passe. Le nom est modifiable, sinon on le deduit du domaine.
        "vault_enabled": bool(raw.get("vault_enabled")) and not raw.get("existing_only"),
        "vault_name": (raw.get("vault_name") or "").strip(),
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


# Caracteres refuses par SharePoint dans un nom de dossier (« / » sert de
# separateur de niveaux et reste donc autorise dans le chemin).
INVALID_FOLDER_CHARS = set(r'"*:<>?\|')


def clean_folder_path(path: str) -> str:
    """Chemin de dossier utilisable, ou chaine vide s'il est inexploitable."""
    parts = []
    for part in (path or "").split("/"):
        part = part.strip().strip(".")
        if not part or any(c in INVALID_FOLDER_CHARS for c in part):
            return ""
        parts.append(part)
    return "/".join(parts)


async def create_site_folders(
    ctx: JobContext, graph: GraphClient, site: dict, folders: list[str]
) -> None:
    """Cree l'arborescence demandee dans la bibliotheque du nouveau site."""
    if not folders:
        return

    drive = await graph.default_site_drive(site["id"])
    if not drive:
        drives = await graph.site_drives(site["id"])
        drive = drives[0] if drives else None
    if not drive:
        ctx.warn("sharepoint", "Bibliotheque introuvable : aucun dossier cree.")
        return

    for raw in folders:
        path = clean_folder_path(raw)
        if not path:
            ctx.warn("sharepoint", f"Nom de dossier refuse par SharePoint : « {raw} »")
            continue
        try:
            await graph.ensure_folder(drive["id"], path)
            ctx.success("sharepoint", f"Dossier « {path} » cree.")
        except GraphError as exc:
            ctx.error("sharepoint", f"Creation du dossier « {path} » impossible : {exc.friendly}")


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
            "onedrive": "en attente" if spec.get("provision_onedrive") else "non demande",
            "shortcut": "en attente",
            "shortcut_folders": list(spec.get("shortcut_folders") or []),
            "shortcuts": [],
            "provision_onedrive": bool(spec.get("provision_onedrive")),
            "vault_enabled": bool(spec.get("vault_enabled")),
            "vault_name": spec.get("vault_name") or "",
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
    # Seuls les comptes pour lesquels l'operateur l'a demande — ou qui
    # attendent un raccourci — passent par cette etape, la plus lente.
    targets = [r for r in results if r.get("id") and r.get("provision_onedrive")]
    if not targets:
        ctx.info("onedrive", "Aucun OneDrive a provisionner.")
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
    for attempt in range(0, ONEDRIVE_ATTEMPTS + 1):
        if not pending:
            break
        # La 1re passe est immediate : un compte en place a deja son OneDrive,
        # inutile de lui faire attendre un cycle complet.
        if attempt:
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
    default_label: str,
) -> None:
    """Pose les raccourcis demandes, un utilisateur pouvant en recevoir plusieurs.

    Les cibles sont resolues une seule fois par dossier : dix utilisateurs
    pointant sur « Comptabilite » ne declenchent qu'un aller-retour Graph.
    """
    targets: dict[str, dict | None] = {}

    async def target_for(folder: str) -> dict | None:
        if folder not in targets:
            targets[folder] = await resolve_shortcut_target(ctx, graph, site, folder)
        return targets[folder]

    for entry in results:
        folders = entry.get("shortcut_folders") or []
        if not folders:
            entry["shortcut"] = "non demande"
            continue

        drive_id = entry.get("drive_id")
        if not drive_id:
            entry["shortcut"] = "impossible (OneDrive absent)"
            entry["shortcuts"] = [
                {"folder": f or "(racine)", "status": "OneDrive absent"} for f in folders
            ]
            continue

        # Lu une fois par utilisateur, puis tenu a jour au fil des ajouts.
        existing = await sharepoint.existing_shortcut_names(graph, drive_id)
        done: list[dict] = []

        for folder in folders:
            # Le raccourci prend le nom du dossier vise, sinon celui du site.
            label = folder.rsplit("/", 1)[-1] if folder else default_label
            shown = folder or "(racine)"

            # Verifie avant de resoudre la cible : un raccourci deja en place
            # ne doit pas couter d'aller-retour Graph.
            if label.casefold() in existing:
                done.append({"folder": shown, "status": "deja present"})
                ctx.info("raccourcis", f"Raccourci « {label} » deja present chez {entry['upn']}")
                continue

            target = await target_for(folder)
            if not target:
                done.append({"folder": shown, "status": "cible introuvable"})
                entry["errors"].append(f"raccourci : dossier « {shown} » introuvable")
                continue

            try:
                await sharepoint.add_shortcut(
                    graph, drive_id, target["driveId"], target["itemId"], label
                )
                existing.add(label.casefold())
                done.append({"folder": shown, "status": "ajoute"})
                ctx.success("raccourcis", f"Raccourci « {label} » ajoute chez {entry['upn']}")
            except (sharepoint.SharePointError, GraphError) as exc:
                done.append({"folder": shown, "status": "echec"})
                entry["errors"].append(f"raccourci « {shown} » : {exc}")
                ctx.warn(
                    "raccourcis", f"Raccourci « {label} » impossible pour {entry['upn']} : {exc}"
                )

        entry["shortcuts"] = done
        statuses = {d["status"] for d in done}
        if statuses == {"ajoute"}:
            entry["shortcut"] = f"{len(done)} ajoute(s)"
        elif statuses == {"deja present"}:
            entry["shortcut"] = "deja presents"
        else:
            entry["shortcut"] = ", ".join(sorted(statuses))


# ---------------------------------------------------------------------------
# Etape 5 : Bitwarden
# ---------------------------------------------------------------------------
async def store_in_vault(
    ctx: JobContext, results: list[dict], tenant: dict, vault_spec: dict
) -> None:
    wanted = [e for e in results if e.get("vault_enabled")]
    for entry in results:
        if not entry.get("vault_enabled"):
            entry["vault"] = "non demande"
    if not wanted:
        ctx.info("bitwarden", "Aucun identifiant a deposer dans le coffre.")
        return

    ready, message = await bitwarden.is_ready()
    if not ready:
        for entry in wanted:
            entry["vault"] = "indisponible"
            entry["password_shown"] = True
        ctx.error(
            "bitwarden",
            f"Coffre indisponible : {message} — les mots de passe sont affiches "
            "dans le recapitulatif, a mettre au coffre manuellement.",
        )
        return

    org_id = vault_spec.get("organization_id") or None
    collection_id = vault_spec.get("collection_id") or None
    collection_ids = [collection_id] if collection_id else None
    client_name = tenant.get("display_name") or tenant.get("default_domain") or tenant["id"]

    for entry in wanted:
        if not entry.get("created"):
            entry["vault"] = "ignore (compte non cree)"
            continue
        if not entry.get("password"):
            entry["vault"] = "ignore (pas de mot de passe)"
            continue
        name = entry.get("vault_name") or default_vault_name(
            entry["upn"].split("@")[-1], entry["upn"]
        )
        try:
            notes = (
                f"Compte Microsoft 365 cree par EZ365 le {db.now()}.\n"
                f"Client : {client_name}\nTenant : {tenant['id']}\n"
                f"Licences : {', '.join(entry.get('license_names') or []) or 'aucune'}"
            )
            await bitwarden.create_login(
                name=name,
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
            entry["vault_name"] = name
            entry["password_shown"] = False
            ctx.success("bitwarden", f"« {name} » depose dans le coffre.")
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

    wanted_shortcuts = sum(len(u.get("shortcut_folders") or []) for u in user_specs)
    ctx.info(
        "demarrage",
        f"Tenant {tenant.get('display_name') or tenant['id']} — "
        f"{len(user_specs)} utilisateur(s), site : {site_spec.get('mode')}, "
        f"{wanted_shortcuts} raccourci(s) demande(s).",
    )

    async with GraphClient(tenant["id"]) as graph:
        site = await ensure_site(ctx, graph, site_spec)

        # Les dossiers ne sont crees que sur un site tout neuf : sur un site
        # existant, l'operateur choisit parmi ceux deja en place.
        if site and site_spec.get("mode") in ("team", "communication"):
            await create_site_folders(ctx, graph, site, site_spec.get("folders") or [])

        results = await create_users(ctx, graph, user_specs, site)

        await provision_onedrives(ctx, graph, results)

        if site:
            default_label = site_spec.get("display_name") or "Documents"
            await add_shortcuts(ctx, graph, results, site, default_label)
        elif wanted_shortcuts:
            for entry in results:
                if entry.get("shortcut_folders"):
                    entry["shortcut"] = "impossible (aucun site)"
                    entry["errors"].append(
                        "raccourci : aucun site n'a ete choisi pour ce traitement"
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
