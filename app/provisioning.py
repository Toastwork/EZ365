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

import httpx

from . import db, passwords
from .jobs import JobContext
from .msgraph import sharepoint
from .msgraph.client import GraphClient, GraphError
from .msgraph.oauth import ConsentError
from .vault import bitwarden

log = logging.getLogger(__name__)

from .config import get_settings

# Attente du OneDrive : intervalle entre deux verifications, duree par defaut
# et duree maximale admise depuis le formulaire.
ONEDRIVE_DELAY = 15.0
ONEDRIVE_WAIT_DEFAULT = 120
ONEDRIVE_WAIT_MAX = 600
# Une verification de OneDrive ne doit pas consommer tout le budget d'attente.
ONEDRIVE_CHECK_TIMEOUT = 20


class StepTimeout(Exception):
    """Une etape a depasse son delai : on passe a la suite."""


async def bounded(coro, seconds: float, what: str):
    """Execute `coro` en l'interrompant au-dela de `seconds`."""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError as exc:
        raise StepTimeout(
            f"{what} : pas de reponse de Microsoft apres {int(seconds)} s"
        ) from exc


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
        # Sans son groupe, les comptes traites n'obtiendraient aucun droit sur
        # le site : ils ne le verraient pas et ne pourraient pas le synchroniser.
        group_id = await sharepoint.find_site_group(graph, site)
        if group_id:
            ctx.info(
                "sharepoint",
                "Site d'equipe : les comptes traites seront ajoutes a ses membres.",
            )
        else:
            ctx.warn(
                "sharepoint",
                "Aucun groupe Microsoft 365 trouve pour ce site (site de "
                "communication ou alias renomme) : les comptes n'y seront pas "
                "ajoutes, l'acces est a accorder depuis SharePoint.",
            )
        return {"id": site["id"], "webUrl": site.get("webUrl"), "groupId": group_id}

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
        db.remember_site(ctx.tenant_id, {**site, "displayName": display_name})
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
        db.remember_site(ctx.tenant_id, {**site, "displayName": display_name})
        ctx.warn(
            "sharepoint",
            "Un site de communication n'a pas de groupe : les comptes n'y sont "
            "pas ajoutes automatiquement, l'acces est a accorder depuis SharePoint.",
        )
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
            "site_access": "",
            "errors": [],
        }
        try:
            await bounded(
                _process_user(ctx, graph, spec, entry, site),
                get_settings().step_timeout,
                f"Traitement de {spec['upn']}",
            )
        except StepTimeout as exc:
            await _after_user_timeout(ctx, graph, spec, entry, exc)
        except GraphError as exc:
            entry["errors"].append(exc.friendly)
            ctx.error("utilisateurs", f"Echec sur {spec['upn']} : {exc.friendly}")
        except Exception as exc:  # noqa: BLE001
            entry["errors"].append(str(exc))
            ctx.error("utilisateurs", f"Echec sur {spec['upn']} : {exc}")

        results.append(entry)
    return results


async def _after_user_timeout(
    ctx: JobContext, graph: GraphClient, spec: dict, entry: dict, exc: StepTimeout
) -> None:
    """Delai depasse sur un compte : constater ce qui a ete fait, puis continuer.

    Le plus delicat : la creation a pu aboutir cote Microsoft sans que la
    reponse nous parvienne. Le compte existe alors avec un mot de passe que
    nous sommes seuls a connaitre — on le verifie pour ne pas le perdre.
    """
    entry["errors"].append(str(exc))
    if not entry.get("id") and not spec["existing_only"]:
        try:
            found = await bounded(graph.find_user(spec["upn"]), 15, "Verification")
        except (StepTimeout, GraphError):
            found = None
        if found:
            entry["id"] = found["id"]
            entry["created"] = True
    if entry.get("created"):
        # Le mot de passe reste affiche tant que le coffre ne l'a pas recu.
        entry["password_shown"] = True
        ctx.warn(
            "utilisateurs",
            f"{exc} — le compte {spec['upn']} a bien ete cree, mais les etapes "
            "suivantes (licence, acces au site) sont a verifier. Compte suivant.",
        )
    else:
        ctx.error("utilisateurs", f"{exc} — on passe au compte suivant.")


async def _process_user(
    ctx: JobContext, graph: GraphClient, spec: dict, entry: dict, site: dict | None
) -> None:
    """Creation ou reprise d'un compte, licences, acces au site.

    `entry` est complete au fil de l'eau : si le delai est depasse en cours de
    route, l'appelant sait jusqu'ou le traitement est alle.
    """
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
            entry["site_access"] = "membre ajoute"
            ctx.success("sharepoint", f"{spec['upn']} ajoute aux membres du site.")
        except GraphError as exc:
            # Seul ce 400-la signifie « deja membre » ; les autres sont
            # de vrais refus et doivent se voir.
            if exc.status == 400 and "already exist" in exc.message.lower():
                entry["site_access"] = "deja membre"
            else:
                entry["site_access"] = "echec"
                entry["errors"].append(f"acces au site : {exc.friendly}")
                ctx.error(
                    "sharepoint",
                    f"{spec['upn']} n'a pas pu etre ajoute aux membres du "
                    f"site : {exc.friendly}",
                )
    elif site:
        entry["site_access"] = "non gere"


# ---------------------------------------------------------------------------
# Etape 3 : OneDrive
# ---------------------------------------------------------------------------
async def provision_onedrives(
    ctx: JobContext,
    graph: GraphClient,
    results: list[dict],
    wait_seconds: int = ONEDRIVE_WAIT_DEFAULT,
) -> None:
    """Demande les OneDrive, puis les attend au plus `wait_seconds`.

    Un OneDrive se cree de facon asynchrone, parfois en plusieurs minutes.
    Au-dela du delai choisi, on passe a la suite : le OneDrive finira de se
    creer seul, et un nouveau passage en « utilisateur existant » posera les
    raccourcis manquants.
    """
    # Seuls les comptes pour lesquels l'operateur l'a demande — ou qui
    # attendent un raccourci — passent par cette etape, la plus lente.
    targets = [r for r in results if r.get("id") and r.get("provision_onedrive")]
    if not targets:
        ctx.info("onedrive", "Aucun OneDrive a provisionner.")
        return

    wait_seconds = max(0, min(int(wait_seconds), ONEDRIVE_WAIT_MAX))
    ctx.info(
        "onedrive",
        f"Demande de creation du OneDrive pour {len(targets)} compte(s) "
        f"(attente maximale : {wait_seconds} s).",
    )

    # 1. Demande explicite a SharePoint : la seule voie fiable en app-only.
    raw: dict = {}
    try:
        queued, detail, raw = await bounded(
            sharepoint.enqueue_personal_sites(graph, [e["upn"] for e in targets]),
            60,
            "Demande de creation des OneDrive",
        )
    except (StepTimeout, GraphError, ConsentError, httpx.HTTPError) as exc:
        queued, detail = False, str(exc)
        raw = {"exception": type(exc).__name__, "message": str(exc)}
    if queued:
        ctx.info("onedrive", "Creation des OneDrive demandee a SharePoint.")
    else:
        ctx.warn(
            "onedrive",
            f"Demande de creation des OneDrive refusee : {detail} Repli sur "
            "l'amorce Graph, qui ne cree pas le OneDrive d'un compte jamais "
            "connecte. Detail brut ci-dessous.",
            raw,
        )

    # 2. Amorce Graph, utile a elle seule sur certains tenants.
    for entry in targets:
        try:
            await bounded(graph.user_drive(entry["id"]), ONEDRIVE_CHECK_TIMEOUT, "Amorce")
        except (GraphError, StepTimeout) as exc:
            log.debug("Amorce OneDrive %s : %s", entry["upn"], exc)

    # 3. Attente bornee. Une premiere verification est toujours faite : un
    #    compte existant a generalement deja son OneDrive.
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + wait_seconds
    next_report = started + 60
    pending = {e["upn"]: e for e in targets}
    first = True

    while pending:
        if not first:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(ONEDRIVE_DELAY, remaining))
        first = False

        for upn in list(pending):
            entry = pending[upn]
            try:
                drive = await bounded(
                    graph.user_drive(entry["id"]), ONEDRIVE_CHECK_TIMEOUT, "Lecture"
                )
            except (GraphError, StepTimeout) as exc:
                log.debug("Attente OneDrive %s : %s", upn, exc)
                continue
            if drive and drive.get("id"):
                entry["drive_id"] = drive["id"]
                entry["onedrive"] = "pret"
                ctx.success("onedrive", f"OneDrive pret pour {upn}")
                pending.pop(upn, None)

        now = loop.time()
        if pending and now >= next_report and now < deadline:
            ctx.info(
                "onedrive",
                f"Toujours en attente pour {len(pending)} compte(s) "
                f"({int(now - started)} s sur {wait_seconds} s)…",
            )
            next_report = now + 60
        if now >= deadline:
            break

    for upn, entry in pending.items():
        entry["onedrive"] = "en cours de creation" if queued else "non provisionne"
        if entry.get("shortcut_folders"):
            entry["errors"].append(
                "raccourcis non poses : OneDrive pas encore pret — relancer plus "
                "tard en « utilisateur existant »"
            )
        ctx.warn(
            "onedrive",
            f"OneDrive de {upn} pas pret apres {wait_seconds} s : on passe a la "
            "suite. " + (
                "Sa creation est en cours cote SharePoint."
                if queued else
                "Il sera cree au plus tard a sa premiere connexion."
            ),
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

        limit = get_settings().shortcut_timeout
        # Lu une fois par utilisateur, puis tenu a jour au fil des ajouts.
        try:
            existing = await bounded(
                sharepoint.existing_shortcut_names(graph, drive_id), limit,
                "Lecture du OneDrive",
            )
        except StepTimeout as exc:
            # Sans cette liste, on risque seulement un doublon renomme.
            ctx.warn("raccourcis", f"{exc} ({entry['upn']}) : doublons non verifies.")
            existing = set()
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

            try:
                target = await bounded(target_for(folder), limit, f"Dossier « {shown} »")
            except StepTimeout as exc:
                done.append({"folder": shown, "status": "delai depasse"})
                entry["errors"].append(f"raccourci : {exc}")
                ctx.warn("raccourcis", f"{exc} — raccourci suivant.")
                continue
            if not target:
                done.append({"folder": shown, "status": "cible introuvable"})
                entry["errors"].append(f"raccourci : dossier « {shown} » introuvable")
                continue

            try:
                await bounded(
                    sharepoint.add_shortcut(
                        graph, drive_id, target["driveId"], target["itemId"], label
                    ),
                    limit,
                    f"Raccourci « {label} » pour {entry['upn']}",
                )
                existing.add(label.casefold())
                done.append({"folder": shown, "status": "ajoute"})
                ctx.success("raccourcis", f"Raccourci « {label} » ajoute chez {entry['upn']}")
            except StepTimeout as exc:
                # La creation a pu aboutir sans que la reponse arrive.
                done.append({"folder": shown, "status": "delai depasse"})
                entry["errors"].append(f"raccourci : {exc} (a verifier)")
                ctx.warn("raccourcis", f"{exc} — raccourci suivant, a verifier.")
            except GraphError as exc:
                # Graph reconnait un raccourci vers la meme cible meme sous un
                # autre nom (« Documents » quand l'utilisateur l'a ajoute
                # lui-meme) : ce n'est pas un echec, le raccourci est la.
                if exc.status == 409 and "shortcut already exist" in exc.message.lower():
                    existing.add(label.casefold())
                    done.append({"folder": shown, "status": "deja present"})
                    ctx.info(
                        "raccourcis",
                        f"Raccourci vers « {shown} » deja present chez {entry['upn']} "
                        "(sous un autre nom).",
                    )
                    continue
                done.append({"folder": shown, "status": "echec"})
                entry["errors"].append(f"raccourci « {shown} » : {exc}")
                ctx.warn(
                    "raccourcis", f"Raccourci « {label} » impossible pour {entry['upn']} : {exc}"
                )
            except sharepoint.SharePointError as exc:
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

    limit = get_settings().vault_timeout
    try:
        ready, message = await bounded(bitwarden.is_ready(), limit, "Coffre")
    except StepTimeout as exc:
        ready, message = False, str(exc)
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
            await bounded(bitwarden.create_login(
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
            ), limit, f"Depot de « {name} »")
            entry["vault"] = "enregistre"
            entry["vault_name"] = name
            entry["password_shown"] = False
            ctx.success("bitwarden", f"« {name} » depose dans le coffre.")
        except StepTimeout as exc:
            # L'entree a pu etre creee malgre tout : a verifier avant de la refaire.
            entry["vault"] = "delai depasse"
            entry["password_shown"] = True
            entry["errors"].append(f"coffre : {exc}")
            ctx.error(
                "bitwarden",
                f"{exc} — verifiez le coffre avant de recreer l'entree ; le mot de "
                "passe reste affiche dans le recapitulatif. Entree suivante.",
            )
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
async def run_site_creation(ctx: JobContext, tenant: dict, site_spec: dict) -> dict:
    """Cree le site et son arborescence, sans toucher aux utilisateurs."""
    folders = site_spec.get("folders") or []
    ctx.info(
        "demarrage",
        f"Creation d'un site sur {tenant.get('display_name') or tenant['id']} — "
        f"{len(folders)} dossier(s) demande(s).",
    )
    async with GraphClient(tenant["id"]) as graph:
        site = await ensure_site(ctx, graph, site_spec)
        if site:
            await create_site_folders(ctx, graph, site, folders)
    return {"site": site, "users": [], "created": 0, "total": 0, "has_errors": False}


DEPLOY_MAX_USERS = 1000


def is_deployable(user: dict) -> bool:
    """Compte qui peut recevoir un raccourci : actif, interne, avec licence.

    Les boites partagees et salles n'ont pas de licence, les invites pas de
    OneDrive chez le client : ils sont ecartes du deploiement de masse.
    """
    upn = (user.get("userPrincipalName") or "").lower()
    return (
        bool(upn)
        and user.get("accountEnabled", True) is not False
        and (user.get("userType") or "Member") != "Guest"
        and "#ext#" not in upn
        and bool(user.get("assignedLicenses"))
    )


def merge_deployment(
    everyone: list[dict], mass_folders: list[str], excluded: set[str],
    per_user: list[dict],
) -> list[dict]:
    """Liste finale : dossiers communs pour tous, plus les ajouts individuels.

    Un ajout individuel sur un compte exclu du lot commun reste applique :
    c'est un choix explicite de l'operateur.
    """
    plan: dict[str, dict] = {}

    def add(upn: str, name: str, folders: list[str]) -> None:
        key = upn.lower()
        entry = plan.setdefault(key, {"upn": key, "display_name": name, "folders": []})
        for folder in folders:
            if folder not in entry["folders"]:
                entry["folders"].append(folder)

    if mass_folders:
        for user in everyone:
            upn = (user.get("userPrincipalName") or "").lower()
            if is_deployable(user) and upn not in excluded:
                add(upn, user.get("displayName") or upn, mass_folders)
    for row in per_user:
        if row.get("folders"):
            add(row["upn"], row.get("display_name") or row["upn"], row["folders"])
    return [
        normalize_user(
            {
                "upn": p["upn"],
                "display_name": p["display_name"],
                "shortcut_folders": p["folders"],
                "provision_onedrive": True,
                "existing_only": True,
            },
            p["upn"].split("@")[-1],
            "FR",
        )
        for p in plan.values()
    ]


async def run_deployment(ctx: JobContext, tenant: dict, spec: dict) -> dict:
    """Raccourcis vers un site existant : dossiers communs a tous + ajouts individuels."""
    mass = spec.get("mass_folders") or []
    everyone: list[dict] = []
    if mass:
        async with GraphClient(tenant["id"]) as graph:
            everyone = await graph.list_users("", limit=DEPLOY_MAX_USERS * 2)
        kept = sum(1 for u in everyone if is_deployable(u))
        ctx.info(
            "demarrage",
            f"{len(everyone)} compte(s) lu(s) sur le tenant, {kept} eligible(s) "
            "(actifs, internes, avec licence), "
            f"{len(spec.get('excluded') or [])} exclu(s) par l'operateur.",
        )
    users = merge_deployment(
        everyone, mass, set(spec.get("excluded") or []), spec.get("per_user") or []
    )
    empty = {"site": None, "users": [], "created": 0, "total": 0, "has_errors": False}
    if not users:
        ctx.warn("demarrage", "Aucun compte a traiter.")
        return empty
    if len(users) > DEPLOY_MAX_USERS:
        ctx.error("demarrage", f"{len(users)} comptes : limite de {DEPLOY_MAX_USERS} depassee.")
        return {**empty, "has_errors": True}
    return await run_provisioning(ctx, tenant, {
        "site": spec["site"],
        "users": users,
        "onedrive_wait": spec.get("onedrive_wait", ONEDRIVE_WAIT_DEFAULT),
        "vault": {"enabled": False},
    })


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

        await provision_onedrives(
            ctx, graph, results,
            spec.get("onedrive_wait", ONEDRIVE_WAIT_DEFAULT),
        )

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
