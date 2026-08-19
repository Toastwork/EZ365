"""Test de fumee : demarre l'app en memoire et parcourt les ecrans principaux."""
import os
os.environ.update({
    "AUTH_MODE": "local",
    "EZ365_LOCAL_USERS": "testeur:motdepasse",
    # Cle Fernet jetable, generee pour les tests : ce n'est PAS la cle de production.
    "STORAGE_KEY": "1HkwvGs9HefxF0GEAuFbvOte6qf9zLqPYnQkQpJCsJk=",
    "DATA_DIR": ".localdata",
    "MS_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
    "MS_CLIENT_SECRET": "secret-de-test",
    "MS_REDIRECT_URI": "https://ez365.example/ms/callback",
    "VAULT_ENABLED": "false",
    "VAULT_API_URL": "",
    "SSL_CERTFILE": "", "SSL_KEYFILE": "",
    "LOG_LEVEL": "WARNING",
})

from fastapi.testclient import TestClient
from app.main import app
from app import db

ok = lambda label, cond: print(("  OK   " if cond else "  ECHEC") + f"  {label}")
fails = []
def check(label, cond):
    ok(label, cond)
    if not cond:
        fails.append(label)

with TestClient(app) as client:
    r = client.get("/healthz")
    check(f"/healthz -> {r.status_code} {r.json()['status']}", r.status_code == 200)

    r = client.get("/", follow_redirects=False)
    check("acces anonyme redirige vers /login", r.status_code == 303 and "/login" in r.headers["location"])

    r = client.get("/login")
    check("page de connexion affichee", r.status_code == 200 and "Connexion" in r.text)

    r = client.post("/login", data={"username": "testeur", "password": "faux"})
    check("mauvais mot de passe refuse", r.status_code == 401)

    r = client.post("/login", data={"username": "testeur", "password": "motdepasse"},
                    follow_redirects=False)
    check("connexion acceptee", r.status_code == 303)

    r = client.get("/")
    check("tableau de bord accessible", r.status_code == 200 and "Tenants connectes" in r.text)

    r = client.get("/tenants/connect", follow_redirects=False)
    loc = r.headers.get("location", "")
    check("redirection vers l'admin consent Microsoft",
          r.status_code == 303 and "login.microsoftonline.com/common/adminconsent" in loc)

    state = loc.split("state=")[1].split("&")[0]

    r = client.get("/ms/callback", params={"state": "inconnu", "tenant": "x", "admin_consent": "True"})
    check("state invalide rejete", r.status_code == 400)

    r = client.get("/ms/callback",
                   params={"state": state, "tenant": "11111111-2222-3333-4444-555555555555",
                           "admin_consent": "True"})
    check("consentement enregistre malgre Graph injoignable", r.status_code == 200)

    row = db.query_one("SELECT * FROM tenants")
    check("tenant persiste en base", row is not None and row["id"].startswith("11111111"))

    r = client.get("/tenants/11111111-2222-3333-4444-555555555555")
    check("fiche tenant rendue avec erreur Graph explicite",
          r.status_code == 200 and "Provisionner" in r.text)

    r = client.post("/tenants/11111111-2222-3333-4444-555555555555/provision",
                    data={"site_mode": "none"}, follow_redirects=False)
    check("formulaire vide refuse", r.status_code == 303 and "/tenants/" in r.headers["location"])

    r = client.post("/tenants/11111111-2222-3333-4444-555555555555/provision",
                    data={"site_mode": "none", "first_name": "Marie", "last_name": "Dupont",
                          "alias": "", "job_title": "", "department": "",
                          "user_domain": "client.fr", "usage_location": "FR"},
                    follow_redirects=False)
    check("traitement lance", r.status_code == 303 and "/jobs/" in r.headers["location"])
    job_id = r.headers["location"].rsplit("/", 1)[1]

    r = client.get(f"/jobs/{job_id}")
    check("page de journal accessible", r.status_code == 200)

    r = client.get(f"/jobs/{job_id}/events")
    check("flux d'evenements JSON", r.status_code == 200 and "events" in r.json())

    r = client.get("/tenants/inconnu")
    check("tenant inconnu -> 404 propre", r.status_code == 404)

    r = client.get("/logout", follow_redirects=False)
    check("deconnexion", r.status_code == 303)

    r = client.get("/", follow_redirects=False)
    check("session bien fermee", r.status_code == 303)

print()
print("ECHECS :", fails if fails else "aucun")
raise SystemExit(1 if fails else 0)
