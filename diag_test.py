"""Diagnostic OneDrive : etapes, rapport et page."""
import asyncio
import base64
import json
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

import httpx

from app import diagnostics
from app.msgraph import oauth, sharepoint
from app.msgraph.client import GraphError

fails = []


def check(label, cond, got=None):
    print(("  OK   " if cond else "  ECHEC") + f"  {label}" + ("" if cond else f"  -> {got!r}"))
    if not cond:
        fails.append(label)


def fake_token(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"entete.{body}.signature"


GRAPH_TOKEN = fake_token({"aud": "https://graph.microsoft.com", "appid": "APP",
                          "roles": ["User.ReadWrite.All", "Sites.ReadWrite.All"]})
SP_TOKEN = fake_token({"aud": "https://acme-admin.sharepoint.com", "appid": "APP", "roles": []})


# --- revendications d'un jeton ------------------------------------------------------
claims = diagnostics.token_claims(GRAPH_TOKEN)
check("roles extraits du jeton", claims["roles"] == ["User.ReadWrite.All", "Sites.ReadWrite.All"], claims)
check("jeton illisible signale", diagnostics.token_claims("pas-un-jeton") == {"illisible": True})


class FakeGraph:
    """Compte licencie, OneDrive absent."""
    drive_error = GraphError(404, "itemNotFound", "User's mysite not found.", "GET /users/U/drive")
    licensed = True

    def __init__(self, tenant_id):
        self.tenant_id = tenant_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, path, params=None, **kw):
        if path.startswith("/users/") and path.endswith("/drive"):
            raise FakeGraph.drive_error
        if path.startswith("/users/"):
            if "absent" in path:
                raise GraphError(404, "Request_ResourceNotFound", "absent", "GET")
            return {"id": "U", "userPrincipalName": "neo@acme.fr",
                    "accountEnabled": True, "usageLocation": "FR"}
        raise AssertionError(path)

    async def user_licenses(self, uid):
        state = "Success" if FakeGraph.licensed else "Disabled"
        return [{"skuPartNumber": "O365_BUSINESS_PREMIUM", "servicePlans": [
            {"servicePlanName": "SHAREPOINTSTANDARD", "provisioningStatus": state},
            {"servicePlanName": "EXCHANGE_S_STANDARD", "provisioningStatus": "Success"},
        ]}]

    async def sharepoint_hostname(self):
        return "acme.sharepoint.com"


async def fake_token_for(tenant_id, scope=oauth.GRAPH_SCOPE, force_refresh=False):
    return SP_TOKEN if ".sharepoint.com" in scope else GRAPH_TOKEN


async def enqueue_refused(graph, emails):
    resp = httpx.Response(
        401, text='{"error_description":"Unsupported app only token."}',
        headers={"x-ms-diagnostics": "3001000;reason=\"There has been an error authenticating the request.\"",
                 "request-id": "RID-1"},
        request=httpx.Request("POST", "https://acme-admin.sharepoint.com/x"),
    )
    return "https://acme-admin.sharepoint.com/x", resp


diagnostics.GraphClient = FakeGraph
oauth.get_app_token = fake_token_for
sharepoint.post_enqueue = enqueue_refused


async def scenarios():
    steps = await diagnostics.onedrive_diagnostic("t1", "neo@acme.fr")
    titles = [s.title for s in steps]
    check("six etapes jouees", len(steps) == 6, titles)
    check("jeton Graph OK", steps[0].ok is True)
    check("licence SharePoint active detectee", steps[2].ok is True, steps[2].summary)
    check("OneDrive absent : erreur Graph brute",
          steps[3].ok is False and "mysite not found" in steps[3].summary, steps[3].summary)
    check("jeton SharePoint sans role signale",
          "AUCUN" in steps[4].summary, steps[4].summary)
    enqueue = steps[5]
    check("refus SharePoint capture tel quel",
          enqueue.ok is False and enqueue.detail["status"] == 401
          and "Unsupported app only token" in enqueue.detail["body"]
          and enqueue.detail["request-id"] == "RID-1", enqueue.detail)

    report = diagnostics.as_text("t1", "neo@acme.fr", steps)
    check("rapport complet", "x-ms-diagnostics" in report and "Unsupported app only token" in report)
    check("aucun jeton dans le rapport", GRAPH_TOKEN not in report and SP_TOKEN not in report)

    FakeGraph.licensed = False
    steps = await diagnostics.onedrive_diagnostic("t1", "neo@acme.fr")
    check("plan SharePoint desactive signale", steps[2].ok is False, steps[2].summary)
    FakeGraph.licensed = True

    steps = await diagnostics.onedrive_diagnostic("t1", "absent@acme.fr")
    check("compte introuvable : arret apres l'etape compte",
          len(steps) == 2 and steps[1].ok is False, [s.title for s in steps])

    async def enqueue_ok(graph, emails):
        return "https://x", httpx.Response(200, json={}, request=httpx.Request("POST", "https://x"))
    sharepoint.post_enqueue = enqueue_ok
    steps = await diagnostics.onedrive_diagnostic("t1", "neo@acme.fr")
    check("demande acceptee", steps[5].ok is True and "acceptee" in steps[5].summary, steps[5].summary)
    sharepoint.post_enqueue = enqueue_refused


asyncio.run(scenarios())

# --- page ----------------------------------------------------------------------------------
shutil.rmtree(".localdata", ignore_errors=True)
from fastapi.testclient import TestClient
from app.main import app
from app import db

with TestClient(app) as client:
    client.post("/login", data={"username": "testeur", "password": "motdepasse"})
    db.execute("INSERT OR IGNORE INTO tenants(id, display_name, default_domain, consented_at, status)"
               " VALUES ('t1','Acme','acme.fr',?,'ok')", (db.now(),))

    page = client.get("/tenants/t1/diagnostic").text
    check("formulaire de diagnostic", 'name="upn"' in page)

    page = client.post("/tenants/t1/diagnostic", data={"upn": "neo@acme.fr"}).text
    check("rapport affiche", 'id="diag-report"' in page and "Unsupported app only token" in page)
    check("verdicts affiches", "ECHEC" in page and "OK" in page)
    check("lien depuis la fiche client", "/tenants/t1/diagnostic" in client.get("/tenants/t1").text)

    anonymous = TestClient(app)
    r = anonymous.post("/tenants/t1/diagnostic", data={"upn": "x"}, follow_redirects=False)
    check("diagnostic reserve aux techniciens", r.status_code == 303, r.status_code)

print()
print("ECHECS :", fails if fails else "aucun")
raise SystemExit(1 if fails else 0)
