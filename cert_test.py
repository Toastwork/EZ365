"""Certificat SharePoint : generation, assertion signee, choix du mode d'authentification."""
import asyncio
import base64
import json
import os
import shutil
import tempfile

os.environ.update({
    "AUTH_MODE": "local", "EZ365_LOCAL_USERS": "testeur:motdepasse",
    # Cle Fernet jetable, generee pour les tests : ce n'est PAS la cle de production.
    "STORAGE_KEY": "1HkwvGs9HefxF0GEAuFbvOte6qf9zLqPYnQkQpJCsJk=",
    "DATA_DIR": ".localdata", "MS_CLIENT_ID": "11111111-aaaa-bbbb-cccc-222222222222",
    "MS_CLIENT_SECRET": "secret-de-test",
    "MS_REDIRECT_URI": "http://localhost:8000/ms/callback",
    "VAULT_ENABLED": "false", "SSL_CERTFILE": "", "SSL_KEYFILE": "", "LOG_LEVEL": "CRITICAL",
})

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.msgraph import certificate, oauth, sharepoint

fails = []


def check(label, cond, got=None):
    print(("  OK   " if cond else "  ECHEC") + f"  {label}" + ("" if cond else f"  -> {got!r}"))
    if not cond:
        fails.append(label)


def b64dec(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


# --- generation et rechargement ----------------------------------------------
shutil.rmtree(".localdata", ignore_errors=True)
check("aucun certificat avant le premier besoin", certificate.load(create=False) is None)
first = certificate.load(create=True)
check("certificat genere", first is not None and first.source == "genere", first)
check("empreinte SHA-1 hexadecimale", len(first.thumbprint) == 40, first.thumbprint)
check("validite de deux ans", 725 <= first.days_left <= 730, first.days_left)

key_pem = open(os.path.join(".localdata", certificate.KEY_FILE), "rb").read()
check("cle privee chiffree sur disque", b"ENCRYPTED" in key_pem, key_pem[:40])
again = certificate.load(create=True)
check("rechargement : meme certificat", again.thumbprint == first.thumbprint)

cer = x509.load_der_x509_certificate(first.public_der())
check(".cer lisible (DER)", cer.fingerprint(hashes.SHA1()).hex().upper() == first.thumbprint)

renewed = certificate.regenerate()
check("regeneration : nouvelle empreinte", renewed.thumbprint != first.thumbprint)

# --- assertion client ----------------------------------------------------------
url = "https://login.microsoftonline.com/tid/oauth2/v2.0/token"
jwt = certificate.client_assertion(renewed, url, "cid")
parts = jwt.split(".")
check("assertion en trois parties", len(parts) == 3, len(parts))
header, claims = json.loads(b64dec(parts[0])), json.loads(b64dec(parts[1]))
check("en-tete RS256 + x5t",
      header["alg"] == "RS256"
      and b64dec(header["x5t"]).hex().upper() == renewed.thumbprint, header)
check("revendications attendues",
      claims["aud"] == url and claims["iss"] == claims["sub"] == "cid"
      and claims["exp"] > claims["nbf"] and claims["jti"], claims)
try:
    renewed.cert.public_key().verify(
        b64dec(parts[2]), f"{parts[0]}.{parts[1]}".encode(),
        padding.PKCS1v15(), hashes.SHA256(),
    )
    check("signature verifiee par la cle publique", True)
except Exception as exc:  # noqa: BLE001
    check("signature verifiee par la cle publique", False, exc)

# --- certificat fourni par l'exploitant -----------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    pem = os.path.join(tmp, "fourni.pem")
    with open(pem, "wb") as handle:
        handle.write(renewed.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"motdepasse")))
        handle.write(renewed.cert.public_bytes(serialization.Encoding.PEM))
    ext = certificate._load_external(pem, "motdepasse")
    check("certificat externe charge", ext.source == "fourni" and ext.thumbprint == renewed.thumbprint)
    try:
        certificate._load_external(pem, "mauvais")
        check("mauvais mot de passe refuse", False)
    except certificate.CertificateError as exc:
        check("mauvais mot de passe refuse", "MS_CERT_PASSWORD" in str(exc), str(exc))


# --- secret pour Graph, certificat pour SharePoint --------------------------------
class FakeClient:
    sent = []
    reply = (200, {"access_token": "jeton", "expires_in": 3600})

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, data=None, **kw):
        FakeClient.sent.append(data)
        status, body = FakeClient.reply
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


async def token_tests():
    real = oauth.httpx.AsyncClient
    oauth.httpx.AsyncClient = FakeClient
    try:
        oauth.invalidate_all()
        await oauth.get_app_token("t1")
        graph_req = FakeClient.sent[-1]
        check("Graph : secret client",
              graph_req.get("client_secret") == "secret-de-test"
              and "client_assertion" not in graph_req, graph_req)

        await oauth.get_app_token("t1", scope="https://acskm-admin.sharepoint.com/.default")
        sp_req = FakeClient.sent[-1]
        check("SharePoint : assertion signee, pas de secret",
              "client_secret" not in sp_req
              and sp_req["client_assertion_type"].endswith("jwt-bearer")
              and sp_req["client_assertion"].count(".") == 2, sp_req)

        # certificat inconnu d'Azure : message qui dit quoi faire
        oauth.invalidate_all()
        FakeClient.reply = (401, {
            "error": "invalid_client",
            "error_description": "AADSTS700027: Client assertion failed signature validation.",
        })
        try:
            await oauth.get_app_token("t2", scope="https://acskm.sharepoint.com/.default")
            check("certificat non depose explique", False)
        except oauth.ConsentError as exc:
            check("certificat non depose explique", "Certificats & secrets" in str(exc), str(exc))
    finally:
        oauth.httpx.AsyncClient = real
        FakeClient.reply = (200, {"access_token": "jeton", "expires_in": 3600})


asyncio.run(token_tests())

# --- refus SharePoint traduits -------------------------------------------------------
req = httpx.Request("POST", "https://x")
msg = sharepoint.explain_sharepoint_refusal(httpx.Response(
    401, text='{"error_description":"Unsupported app only token."}', request=req))
check("401 secret refuse -> certificat", "certificat" in msg, msg)
msg = sharepoint.explain_sharepoint_refusal(httpx.Response(401, text="", request=req))
check("401 autre -> depot ou consentement", "depose" in msg, msg)
msg = sharepoint.explain_sharepoint_refusal(httpx.Response(403, text="Access denied", request=req))
check("403 -> permission Sites.FullControl.All", "Sites.FullControl.All" in msg, msg)

# --- pages ------------------------------------------------------------------------------
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    client.post("/login", data={"username": "testeur", "password": "motdepasse"})
    current = certificate.load(create=False)

    page = client.get("/settings/certificate").text
    check("page : empreinte affichee", current.thumbprint in page)
    check("page : procedure Azure", "Sites.FullControl.All" in page and "Certificats" in page)

    r = client.get("/settings/certificate.cer")
    got = x509.load_der_x509_certificate(r.content).fingerprint(hashes.SHA1()).hex().upper()
    check("telechargement du .cer", r.status_code == 200 and got == current.thumbprint, got)
    check("nom de fichier propose", "ez365-sharepoint.cer" in r.headers.get("content-disposition", ""))

    health = client.get("/healthz").json()
    check("/healthz : certificat decrit",
          health["sharepoint_certificate"]["thumbprint"] == current.thumbprint, health)

    r = client.post("/settings/certificate/regenerate", follow_redirects=False)
    check("regeneration depuis la page",
          r.status_code == 303 and certificate.load(create=False).thumbprint != current.thumbprint)

    anonymous = TestClient(app)
    r = anonymous.get("/settings/certificate.cer", follow_redirects=False)
    check("telechargement reserve aux techniciens", r.status_code == 303, r.status_code)

print()
print("ECHECS :", fails if fails else "aucun")
raise SystemExit(1 if fails else 0)
