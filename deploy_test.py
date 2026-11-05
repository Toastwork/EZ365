"""Deploiement de raccourcis : comptes retenus, formulaire et page."""
import asyncio
import os
import shutil

os.environ.update({
    "AUTH_MODE": "local", "EZ365_LOCAL_USERS": "testeur:motdepasse",
    # Cle Fernet jetable, generee pour les tests : ce n'est PAS la cle de production.
    "STORAGE_KEY": "1HkwvGs9HefxF0GEAuFbvOte6qf9zLqPYnQkQpJCsJk=",
    "DATA_DIR": ".localdata", "MS_CLIENT_ID": "x", "MS_CLIENT_SECRET": "x",
    "MS_REDIRECT_URI": "http://localhost:8000/ms/callback",
    "VAULT_ENABLED": "false", "SSL_CERTFILE": "", "SSL_KEYFILE": "", "LOG_LEVEL": "CRITICAL",
})

from app import provisioning

fails = []


def check(label, cond, got=None):
    print(("  OK   " if cond else "  ECHEC") + f"  {label}" + ("" if cond else f"  -> {got!r}"))
    if not cond:
        fails.append(label)


LIC = [{"skuId": "s1"}]
EVERYONE = [
    {"userPrincipalName": "Alice@c.fr", "displayName": "Alice", "assignedLicenses": LIC},
    {"userPrincipalName": "bob@c.fr", "displayName": "Bob", "assignedLicenses": LIC},
    {"userPrincipalName": "accueil@c.fr", "displayName": "Accueil", "assignedLicenses": []},
    {"userPrincipalName": "old@c.fr", "displayName": "Ancien", "assignedLicenses": LIC,
     "accountEnabled": False},
    {"userPrincipalName": "x_y.fr#EXT#@c.onmicrosoft.com", "displayName": "Invite",
     "assignedLicenses": LIC, "userType": "Guest"},
    {"userPrincipalName": "zoe@c.fr", "displayName": "Zoe", "assignedLicenses": LIC},
]

# --- comptes eligibles ------------------------------------------------------------
eligible = [u["userPrincipalName"] for u in EVERYONE if provisioning.is_deployable(u)]
check("eligibles : actifs, internes, avec licence",
      eligible == ["Alice@c.fr", "bob@c.fr", "zoe@c.fr"], eligible)

# --- fusion pour tous + individuel ------------------------------------------------
plan = provisioning.merge_deployment(
    EVERYONE, ["Commun", ""], {"zoe@c.fr"},
    [{"upn": "bob@c.fr", "display_name": "Bob", "folders": ["Compta", "Commun"]},
     {"upn": "zoe@c.fr", "display_name": "Zoe", "folders": ["Direction"]},
     {"upn": "accueil@c.fr", "display_name": "Accueil", "folders": ["Accueil"]}],
)
by_upn = {u["upn"]: u for u in plan}
check("upn en minuscules", "alice@c.fr" in by_upn, sorted(by_upn))
check("dossiers communs pour tous", by_upn["alice@c.fr"]["shortcut_folders"] == ["Commun", ""])
check("ajout individuel sans doublon",
      by_upn["bob@c.fr"]["shortcut_folders"] == ["Commun", "", "Compta"],
      by_upn["bob@c.fr"]["shortcut_folders"])
check("exclu du commun garde son ajout individuel",
      by_upn["zoe@c.fr"]["shortcut_folders"] == ["Direction"], by_upn["zoe@c.fr"])
check("choix individuel explicite sur un compte sans licence",
      by_upn["accueil@c.fr"]["shortcut_folders"] == ["Accueil"])
check("comptes existants seulement, OneDrive demande, pas de coffre",
      all(u["existing_only"] and u["provision_onedrive"] and not u["vault_enabled"]
          and not u["sku_ids"] for u in plan))
check("inactif et invite ecartes", "old@c.fr" not in by_upn and len(plan) == 4, sorted(by_upn))

only_custom = provisioning.merge_deployment(
    EVERYONE, [], set(), [{"upn": "bob@c.fr", "display_name": "", "folders": ["RH"]}])
check("sans dossier commun : seulement les individuels",
      [u["upn"] for u in only_custom] == ["bob@c.fr"], only_custom)


# --- traitement : la liste complete n'est lue que si un dossier commun est choisi ---
class FakeGraph:
    reads = 0

    def __init__(self, tenant_id):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def list_users(self, search="", limit=200):
        FakeGraph.reads += 1
        return EVERYONE


class Ctx:
    def __init__(self):
        self.lines = []

    def info(self, step, msg, detail=None):
        self.lines.append(msg)

    warn = error = success = info


captured = {}


async def fake_run(ctx, tenant, spec):
    captured.update(spec)
    return {"users": spec["users"]}


provisioning.GraphClient = FakeGraph
real_run = provisioning.run_provisioning
provisioning.run_provisioning = fake_run


async def scenario():
    await provisioning.run_deployment(Ctx(), {"id": "t1"}, {
        "site": {"mode": "existing", "site_id": "S"}, "mass_folders": ["Commun"],
        "excluded": ["bob@c.fr"], "per_user": [], "onedrive_wait": 60})
    check("liste lue pour le deploiement commun", FakeGraph.reads == 1)
    check("site et attente transmis",
          captured["site"]["site_id"] == "S" and captured["onedrive_wait"] == 60)
    check("exclusion appliquee",
          [u["upn"] for u in captured["users"]] == ["alice@c.fr", "zoe@c.fr"], captured["users"])

    ctx = Ctx()
    out = await provisioning.run_deployment(ctx, {"id": "t1"}, {
        "site": {"mode": "existing", "site_id": "S"}, "mass_folders": [], "per_user": []})
    check("rien a faire : aucun appel", FakeGraph.reads == 1 and out["total"] == 0)


asyncio.run(scenario())
provisioning.run_provisioning = real_run

# --- acces a un site sans groupe Microsoft 365 --------------------------------------
from app.msgraph import sharepoint as sp  # noqa: E402

check("racine REST d'un site",
      sp.site_rest_root("https://c.sharepoint.com/sites/FSPPMA/")
      == ("c.sharepoint.com", "https://c.sharepoint.com/sites/FSPPMA"),
      sp.site_rest_root("https://c.sharepoint.com/sites/FSPPMA/"))
check("adresse vide refusee", sp.site_rest_root("") is None)


async def access_scenarios():
    entries = [
        {"upn": "a@c.fr", "id": "A", "site_access": "non gere", "errors": []},
        {"upn": "b@c.fr", "id": "B", "site_access": "non gere", "errors": []},
        {"upn": "c@c.fr", "id": "C", "site_access": "deja membre", "errors": []},
    ]
    vus = {}

    async def fake_grant(graph, web_url, upns):
        vus["url"], vus["upns"] = web_url, list(upns)
        return ["a@c.fr"], {"b@c.fr": "HTTP 403 refus"}

    real = sp.grant_site_access
    sp.grant_site_access = fake_grant
    try:
        await provisioning.grant_site_access(
            Ctx(), FakeGraph("t1"), entries, {"webUrl": "https://c.sharepoint.com/sites/X"})
    finally:
        sp.grant_site_access = real

    check("seuls les comptes sans acces sont traites", vus["upns"] == ["a@c.fr", "b@c.fr"], vus)
    check("acces accorde note dans le recapitulatif", entries[0]["site_access"] == "acces accorde")
    check("refus note avec sa raison",
          entries[1]["site_access"] == "echec"
          and any("403" in e for e in entries[1]["errors"]), entries[1])
    check("compte deja membre laisse tel quel", entries[2]["site_access"] == "deja membre")


asyncio.run(access_scenarios())


# --- commande de repli PowerShell ---------------------------------------------------
cmd = provisioning.powershell_provisioning("c-admin.sharepoint.com", ["b@c.fr", "a@c.fr"])
check("commande : connexion au centre d'administration",
      "Connect-SPOService -Url https://c-admin.sharepoint.com" in cmd, cmd)
check("commande : adresses triees et citees",
      'Request-SPOPersonalSite -UserEmails @("a@c.fr","b@c.fr") -NoWait' in cmd, cmd)
check("commande vide sans compte", provisioning.powershell_provisioning("c-admin", []) == "")


# --- comptes restes sans raccourci --------------------------------------------------
from app.routers.provisioning import unfinished_users  # noqa: E402

resume = {"users": [
    {"upn": "a@c.fr", "display_name": "A", "shortcut_folders": ["Commun", "RH"],
     "shortcuts": [{"folder": "Commun", "status": "ajoute"}]},
    {"upn": "b@c.fr", "display_name": "B", "shortcut_folders": [""],
     "shortcuts": [{"folder": "(racine)", "status": "deja present"}]},
    {"upn": "c@c.fr", "display_name": "C", "shortcut_folders": ["Direction"],
     "shortcuts": [{"folder": "Direction", "status": "OneDrive absent"}]},
    {"upn": "d@c.fr", "display_name": "D", "shortcut_folders": [], "shortcuts": []},
]}
reste = unfinished_users(resume)
check("reprise : seuls les dossiers manquants",
      reste == [{"upn": "a@c.fr", "display_name": "A", "folders": ["RH"]},
                {"upn": "c@c.fr", "display_name": "C", "folders": ["Direction"]}], reste)
check("reprise : rien a faire sur un traitement complet",
      unfinished_users({"users": [resume["users"][1], resume["users"][3]]}) == [])


# --- formulaire et page -------------------------------------------------------------
shutil.rmtree(".localdata", ignore_errors=True)
from fastapi.testclient import TestClient
from app.main import app
from app import db, jobs

launched = []
jobs.launch = lambda job_id, tenant_id, actor, runner: launched.append(job_id)

with TestClient(app) as client:
    client.post("/login", data={"username": "testeur", "password": "motdepasse"})
    db.execute("INSERT OR IGNORE INTO tenants(id, display_name, default_domain, consented_at, status)"
               " VALUES ('t1','Client','c.fr',?,'ok')", (db.now(),))

    page = client.get("/tenants/t1").text
    check("carte de deploiement en tete",
          'id="deploy"' in page and page.index('id="deploy"') < page.index("Creer un site ou des comptes"))
    check("le reste est replie", '<details class="more-actions">' in page
          and page.index('class="more-actions"') < page.index("Destination Bitwarden"))
    check("script de deploiement charge", "/static/deploy.js" in page)

    r = client.post("/tenants/t1/deploy", data={"mass_folders": '["Commun"]'}, follow_redirects=False)
    check("site obligatoire", r.status_code == 303 and r.headers["location"] == "/tenants/t1")

    r = client.post("/tenants/t1/deploy", data={"site_id": "S", "mass_folders": "[]"},
                    follow_redirects=False)
    check("aucun dossier : refuse", r.headers["location"] == "/tenants/t1" and not launched)

    r = client.post("/tenants/t1/deploy", data={
        "site_id": "S", "site_name": "Documents", "mass_folders": '["Commun", ""]',
        "exclude_upn": ["Zoe@c.fr"],
        "deploy_upn": ["bob@c.fr", "ann@c.fr"], "deploy_name": ["Bob", "Ann"],
        "deploy_folders": ['["Compta"]', "[]"], "onedrive_wait": "9999",
    }, follow_redirects=False)
    check("deploiement lance", "/jobs/" in r.headers.get("location", "") and len(launched) == 1,
          r.headers.get("location"))
    spec = jobs.job_payload(launched[0])
    check("dossiers communs lus", spec["mass_folders"] == ["Commun", ""], spec)
    check("exclusion en minuscules", spec["excluded"] == ["zoe@c.fr"], spec["excluded"])
    check("seules les lignes avec dossiers", spec["per_user"] == [
        {"upn": "bob@c.fr", "display_name": "Bob", "folders": ["Compta"]}], spec["per_user"])
    check("attente bornee", spec["onedrive_wait"] == 600, spec["onedrive_wait"])
    check("site transmis", spec["site"] == {"mode": "existing", "site_id": "S",
                                            "display_name": "Documents",
                                            "grant_access": True}, spec["site"])

    launched.clear()
    client.post("/tenants/t1/deploy", data={
        "site_id": "S", "mass_folders": '["Commun"]', "grant_access": "0"},
        follow_redirects=False)
    check("acces au site refusable",
          jobs.job_payload(launched[0])["site"]["grant_access"] is False)

    # reprise d'un traitement : nouveau job pour les seuls comptes restants
    launched.clear()
    job_id = jobs.create_job("t1", "deploiement de raccourcis", "testeur", {
        "site": {"mode": "existing", "site_id": "S", "display_name": "Docs"},
        "onedrive_wait": 60})
    jobs.set_summary(job_id, resume)
    r = client.post(f"/tenants/t1/jobs/{job_id}/retry", follow_redirects=False)
    check("reprise lancee", "/jobs/" in r.headers.get("location", "") and len(launched) == 1,
          r.headers.get("location"))
    repris = jobs.job_payload(launched[0])
    check("reprise : site et comptes repris",
          repris["site"]["site_id"] == "S"
          and [u["upn"] for u in repris["per_user"]] == ["a@c.fr", "c@c.fr"], repris)
    check("reprise : aucun dossier commun rejoue", repris["mass_folders"] == [])

    launched.clear()
    complet = jobs.create_job("t1", "deploiement de raccourcis", "testeur", {"site": {}})
    jobs.set_summary(complet, {"users": [resume["users"][1]]})
    r = client.post(f"/tenants/t1/jobs/{complet}/retry", follow_redirects=False)
    check("reprise inutile : pas de nouveau traitement",
          r.headers.get("location") == f"/jobs/{complet}" and not launched, r.headers.get("location"))

    db.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,))
    page = client.get(f"/jobs/{job_id}").text
    check("bouton de reprise sur la page du traitement",
          f"/tenants/t1/jobs/{job_id}/retry" in page)


print()
print("ECHECS :", fails if fails else "aucun")
raise SystemExit(1 if fails else 0)
