"""Verifie la resolution des licences telle qu'elle sort du formulaire."""
import os
os.environ.update({
    "AUTH_MODE": "local", "EZ365_LOCAL_USERS": "testeur:motdepasse",
    "STORAGE_KEY": "1HkwvGs9HefxF0GEAuFbvOte6qf9zLqPYnQkQpJCsJk=",
    "DATA_DIR": ".localdata", "MS_CLIENT_ID": "x", "MS_CLIENT_SECRET": "x",
    "MS_REDIRECT_URI": "http://localhost:8000/ms/callback",
    "VAULT_ENABLED": "false", "SSL_CERTFILE": "", "SSL_KEYFILE": "", "LOG_LEVEL": "CRITICAL",
})
from fastapi.testclient import TestClient
from app.main import app
from app import db, jobs
from app.routers.provisioning import split_sku

fails = []
def check(label, cond, got=None):
    print(("  OK   " if cond else "  ECHEC") + f"  {label}" + ("" if cond else f"  -> {got!r}"))
    if not cond: fails.append(label)

check("split_sku vide", split_sku("") == ([], []), split_sku(""))
check("split_sku id+libelle", split_sku("abc|BUSINESS") == (["abc"], ["BUSINESS"]), split_sku("abc|BUSINESS"))
check("split_sku sans libelle", split_sku("abc") == (["abc"], ["abc"]), split_sku("abc"))

with TestClient(app) as client:
    client.post("/login", data={"username": "testeur", "password": "motdepasse"})
    db.execute("INSERT OR IGNORE INTO tenants(id, display_name, default_domain, consented_at, status)"
               " VALUES ('t1','Client','c.fr',?,'ok')", (db.now(),))

    # httpx encode les cles repetees a partir d'un dictionnaire de listes ;
    # une liste de tuples serait prise pour un flux binaire.
    form = {
        "site_mode": "none", "user_domain": "c.fr", "usage_location": "FR",
        "default_sku": "sku-std|BUSINESS_STANDARD",
        "first_name": ["Marie", "Jean", "Zoe"],
        "last_name": ["Dupont", "Martin", "Bernard"],
        "alias": ["", "", ""],
        "job_title": ["", "", ""],
        "department": ["", "", ""],
        # sans choix -> defaut | licence propre | explicitement aucune
        "user_sku": ["", "sku-prem|BUSINESS_PREMIUM", "none"],
        # raccourcis : plusieurs dossiers pour Marie, aucun pour Jean
        "user_shortcuts": ['["Compta","RH/Contrats"]', "[]", '[""]'],
        "user_onedrive": ["1", "0", "0"],
        # coffre : nom impose | nom deduit par le serveur | pas de depot
        "user_vault": ["1", "1", "0"],
        "user_vault_name": ["SPECIAL-OFFICE-MD", "", ""],
        # comptes existants : l'un inchange, l'autre a qui on ajoute une licence
        "existing_upn": ["alice@c.fr", "bob@c.fr"],
        "existing_name": ["Alice", "Bob"],
        "existing_sku": ["", "sku-std|BUSINESS_STANDARD"],
        "existing_shortcuts": ['["Direction"]', "[]"],
        "existing_onedrive": ["0", "1"],
    }
    r = client.post("/tenants/t1/provision", data=form, follow_redirects=False)
    check("traitement lance", r.status_code == 303 and "/jobs/" in r.headers["location"],
          r.headers.get("location"))
    if "/jobs/" not in r.headers.get("location", ""):
        page = client.get("/tenants/t1").text
        import re
        msg = re.search(r'class="flash[^"]*">(.*?)</div>', page, re.S)
        print("  message affiche :", (msg.group(1).strip()[:200] if msg else "aucun"))
        raise SystemExit(1)
    job_id = r.headers["location"].rsplit("/", 1)[1]
    users = {u["upn"]: u for u in jobs.job_payload(job_id)["users"]}

    check("5 utilisateurs dans le traitement", len(users) == 5, sorted(users))
    check("licence par defaut appliquee",
          users["marie.dupont@c.fr"]["sku_names"] == ["BUSINESS_STANDARD"], users["marie.dupont@c.fr"])
    check("licence propre a la ligne",
          users["jean.martin@c.fr"]["sku_names"] == ["BUSINESS_PREMIUM"], users["jean.martin@c.fr"])
    check("« aucune » ecrase le defaut",
          users["zoe.bernard@c.fr"]["sku_ids"] == [], users["zoe.bernard@c.fr"])
    check("plusieurs raccourcis sur une ligne",
          users["marie.dupont@c.fr"]["shortcut_folders"] == ["Compta", "RH/Contrats"],
          users["marie.dupont@c.fr"]["shortcut_folders"])
    check("un raccourci force le OneDrive",
          users["marie.dupont@c.fr"]["provision_onedrive"] is True, users["marie.dupont@c.fr"])
    check("ligne sans raccourci ni OneDrive",
          users["jean.martin@c.fr"]["shortcut_folders"] == []
          and users["jean.martin@c.fr"]["provision_onedrive"] is False,
          users["jean.martin@c.fr"])
    check("raccourci sur la racine",
          users["zoe.bernard@c.fr"]["shortcut_folders"] == [""],
          users["zoe.bernard@c.fr"]["shortcut_folders"])
    check("existant sans choix : aucune licence, pas le defaut",
          users["alice@c.fr"]["sku_ids"] == [], users["alice@c.fr"])
    check("existant marque comme tel", users["alice@c.fr"]["existing_only"] is True, users["alice@c.fr"])
    check("existant avec licence demandee",
          users["bob@c.fr"]["sku_names"] == ["BUSINESS_STANDARD"], users["bob@c.fr"])
    check("nom de coffre impose respecte",
          users["marie.dupont@c.fr"]["vault_name"] == "SPECIAL-OFFICE-MD",
          users["marie.dupont@c.fr"]["vault_name"])
    check("nom de coffre deduit du domaine",
          users["jean.martin@c.fr"]["vault_name"] == "C-OFFICE-JEAN.MARTIN",
          users["jean.martin@c.fr"]["vault_name"])
    check("depot refuse sur la 3e fiche",
          users["zoe.bernard@c.fr"]["vault_enabled"] is False, users["zoe.bernard@c.fr"])
    check("compte existant : pas de depot",
          users["alice@c.fr"]["vault_enabled"] is False, users["alice@c.fr"])
    check("raccourci de l'existant conserve",
          users["alice@c.fr"]["shortcut_folders"] == ["Direction"], users["alice@c.fr"])
    check("OneDrive demande seul, sans raccourci",
          users["bob@c.fr"]["provision_onedrive"] is True
          and users["bob@c.fr"]["shortcut_folders"] == [], users["bob@c.fr"])


from app.routers.provisioning import parse_shortcuts
check("parse_shortcuts JSON", parse_shortcuts('["a","b/c"]') == ["a", "b/c"])
check("parse_shortcuts racine", parse_shortcuts('[""]') == [""])
check("parse_shortcuts vide", parse_shortcuts("") == [])
check("parse_shortcuts doublons ecartes", parse_shortcuts('["a","a"]') == ["a"])
check("parse_shortcuts slashs nettoyes", parse_shortcuts('["/a/b/"]') == ["a/b"])
check("parse_shortcuts repli sur |", parse_shortcuts("a|b") == ["a", "b"])
check("parse_shortcuts JSON non liste", parse_shortcuts('{"a":1}') == [])


# --- rendu du bloc coffre selon la disponibilite du sidecar ---------------
import app.vault.bitwarden as bw

async def _ready():
    return (True, "ok")

async def _down():
    return (False, "sidecar injoignable")

# La fiche tenant enchaine sur la lecture des organisations : sans ces
# doublures, l'echec de cet appel ferait retomber vault_ready a False.
async def _orgs():
    return []

async def _collections(organization_id=None):
    return []

bw.organizations = _orgs
bw.collections = _collections

with TestClient(app) as client2:
    client2.post("/login", data={"username": "testeur", "password": "motdepasse"})

    bw.is_ready = _ready
    page = client2.get("/tenants/t1").text
    check("coffre disponible : depot coche par defaut",
          'name="user_vault" value="1"' in page, 'name="user_vault" value="1"' in page)
    vault_block = page.split("user_vault")[1][:400]
    check("coffre disponible : case active",
          "syncVault(this)" in page and "disabled" not in vault_block, vault_block[:80])
    check("champ de nom present", 'name="user_vault_name"' in page)

    bw.is_ready = _down
    page = client2.get("/tenants/t1").text
    check("coffre indisponible : depot desactive",
          'name="user_vault" value="0"' in page and "coffre indisponible" in page,
          'name="user_vault" value="0"' in page)

print()
print("ECHECS :", fails if fails else "aucun")
raise SystemExit(1 if fails else 0)
