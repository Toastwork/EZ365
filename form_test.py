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
        "user_folder": ["Compta", "", "RH"],
        # comptes existants : l'un inchange, l'autre a qui on ajoute une licence
        "existing_upn": ["alice@c.fr", "bob@c.fr"],
        "existing_name": ["Alice", "Bob"],
        "existing_sku": ["", "sku-std|BUSINESS_STANDARD"],
        "existing_folder": ["Direction", ""],
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
    check("dossier par ligne conserve",
          users["marie.dupont@c.fr"]["shortcut_folder"] == "Compta", users["marie.dupont@c.fr"])
    check("existant sans choix : aucune licence, pas le defaut",
          users["alice@c.fr"]["sku_ids"] == [], users["alice@c.fr"])
    check("existant marque comme tel", users["alice@c.fr"]["existing_only"] is True, users["alice@c.fr"])
    check("existant avec licence demandee",
          users["bob@c.fr"]["sku_names"] == ["BUSINESS_STANDARD"], users["bob@c.fr"])
    check("dossier de l'existant conserve",
          users["alice@c.fr"]["shortcut_folder"] == "Direction", users["alice@c.fr"])

print()
print("ECHECS :", fails if fails else "aucun")
raise SystemExit(1 if fails else 0)
