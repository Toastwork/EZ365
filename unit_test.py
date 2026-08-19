"""Verifications unitaires : parsing, normalisation, rendu du recapitulatif."""
import os
os.environ.update({
    "AUTH_MODE": "local", "EZ365_LOCAL_USERS": "testeur:motdepasse",
    # Cle Fernet jetable, generee pour les tests : ce n'est PAS la cle de production.
    "STORAGE_KEY": "1HkwvGs9HefxF0GEAuFbvOte6qf9zLqPYnQkQpJCsJk=",
    "DATA_DIR": ".localdata",
    "MS_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
    "MS_CLIENT_SECRET": "x", "MS_REDIRECT_URI": "http://localhost:8000/ms/callback",
    "VAULT_ENABLED": "false", "SSL_CERTFILE": "", "SSL_KEYFILE": "", "LOG_LEVEL": "WARNING",
})

import json
from app import db, jobs, provisioning
from app.routers.provisioning import parse_bulk
from app.msgraph.sharepoint import slugify, mail_nickname
from app.crypto import encrypt, decrypt

fails = []
def check(label, cond, got=None):
    print(("  OK   " if cond else "  ECHEC") + f"  {label}" + ("" if cond else f"  -> {got!r}"))
    if not cond: fails.append(label)

# --- slug / alias ---------------------------------------------------------
check("slugify accents", slugify("Éléonore Généré") == "eleonore-genere", slugify("Éléonore Généré"))
check("slugify ponctuation", slugify("Doc's & Co. (2024)") == "doc-s-co-2024", slugify("Doc's & Co. (2024)"))
check("mailNickname sans tiret", mail_nickname("Espace Client Nord") == "espaceclientnord", mail_nickname("Espace Client Nord"))

# --- collage en masse -----------------------------------------------------
bulk = parse_bulk("""
# commentaire ignore
Marie;Dupont;marie.dupont;Comptable;Finance
Jean\tMartin\tj.martin\tTechnicien\tIT
Zoe,Bernard,,Assistante,
""")
check("3 lignes parsees", len(bulk) == 3, len(bulk))
check("separateur tabulation", bulk[1]["alias"] == "j.martin", bulk[1])
check("champs manquants tolerees", bulk[2]["department"] == "", bulk[2])

# --- normalisation utilisateur -------------------------------------------
u = provisioning.normalize_user({"first_name": "Éléonore", "last_name": "Généré"}, "client.fr", "FR")
check("UPN derive sans accent", u["upn"] == "eleonore.genere@client.fr", u["upn"])
check("displayName compose", u["display_name"] == "Éléonore Généré", u["display_name"])

u2 = provisioning.normalize_user({"first_name": "Jean", "alias": "jm", "usage_location": "be"}, "client.fr", "FR")
check("alias respecte", u2["upn"] == "jm@client.fr", u2["upn"])
check("usageLocation majuscule", u2["usage_location"] == "BE", u2["usage_location"])

u3 = provisioning.normalize_user({"upn": "DIRECTION@Autre.FR"}, "client.fr", "FR")
check("UPN complet conserve", u3["upn"] == "direction@autre.fr", u3["upn"])

# --- chiffrement ----------------------------------------------------------
secret = "P@ssw0rd-tres-secret"
blob = encrypt(secret)
check("chiffrement reversible", decrypt(blob) == secret)
check("valeur chiffree illisible", secret not in blob)

# --- rendu du recapitulatif ----------------------------------------------
db.connect()
db.execute("INSERT OR IGNORE INTO tenants(id, display_name, consented_at) VALUES ('t1','Client Test',?)", (db.now(),))
job_id = jobs.create_job("t1", "provisionnement", "testeur", {"users": []})
jobs.set_summary(job_id, {
    "created": 2, "total": 3, "has_errors": True,
    "site": {"webUrl": "https://client.sharepoint.com/sites/docs"},
    "users": [
        {"display_name": "Marie Dupont", "upn": "marie.dupont@client.fr", "created": True,
         "existing": False, "license_names": ["SPB"], "onedrive": "pret", "shortcut": "ajoute",
         "vault": "enregistre", "errors": [], "password": ""},
        {"display_name": "Jean Martin", "upn": "j.martin@client.fr", "created": True,
         "existing": False, "license_names": ["SPB"], "onedrive": "non provisionne",
         "shortcut": "impossible (OneDrive absent)", "vault": "echec",
         "errors": ["coffre : sidecar injoignable"], "password": "MotDePasse!42"},
        {"display_name": "Zoe Bernard", "upn": "zoe@client.fr", "created": False,
         "existing": True, "license_names": [], "onedrive": "pret", "shortcut": "deja present",
         "vault": "ignore (compte existant)", "errors": [], "password": ""},
    ],
})
db.execute("UPDATE jobs SET status='error' WHERE id=?", (job_id,))

from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as client:
    client.post("/login", data={"username": "testeur", "password": "motdepasse"})
    r = client.get(f"/jobs/{job_id}")
    check("page journal + recapitulatif", r.status_code == 200 and "Recapitulatif" in r.text)
    check("mot de passe affiche seulement si le coffre a echoue",
          "MotDePasse!42" in r.text and r.text.count("Mot de passe a mettre au coffre") == 1)
    check("erreur utilisateur visible", "sidecar injoignable" in r.text)
    check("lien du site present", "client.sharepoint.com/sites/docs" in r.text)
    r = client.get(f"/jobs/{job_id}/summary")
    check("fragment recapitulatif isole", r.status_code == 200 and "<table" in r.text and "<html" not in r.text)

print()
print("ECHECS :", fails if fails else "aucun")
raise SystemExit(1 if fails else 0)
