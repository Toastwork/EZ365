"""Certificat de l'application, indispensable pour l'API REST de SharePoint.

SharePoint Online refuse les jetons applicatifs obtenus avec un secret client
(« Unsupported app only token ») : il exige un jeton demande avec une
assertion signee par un certificat. Or deux fonctions en dependent — la
creation des OneDrive (CreatePersonalSiteEnqueueBulk) et celle des sites de
communication.

EZ365 genere donc lui-meme ce certificat au premier besoin et le conserve dans
/data, cle privee chiffree avec STORAGE_KEY. Sa partie publique (.cer) se
telecharge depuis l'interface pour etre deposee sur l'application Azure.
Un certificat fourni par l'exploitant (MS_CERT_PATH) prend le pas.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from ..config import get_settings

log = logging.getLogger(__name__)

KEY_FILE = "sharepoint-app.key"
CERT_FILE = "sharepoint-app.crt"
VALIDITY_DAYS = 730

_lock = threading.Lock()


class CertificateError(Exception):
    pass


@dataclass
class AppCertificate:
    key: rsa.RSAPrivateKey
    cert: x509.Certificate
    source: str  # "genere" | "fourni"

    @property
    def thumbprint(self) -> str:
        """Empreinte SHA-1 telle qu'Azure l'affiche."""
        return self.cert.fingerprint(hashes.SHA1()).hex().upper()

    @property
    def not_after(self) -> dt.datetime:
        return self.cert.not_valid_after_utc

    @property
    def days_left(self) -> int:
        return (self.not_after - dt.datetime.now(dt.timezone.utc)).days

    def public_der(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.DER)


def _data_path(name: str) -> str:
    return os.path.join(get_settings().data_dir, name)


def _key_password() -> bytes:
    # Derivee de STORAGE_KEY : la cle privee est illisible sans elle, comme
    # le reste des donnees sensibles de /data.
    return hashlib.sha256(("certificat|" + get_settings().storage_key).encode()).digest()


def _load_external(path: str, password: str) -> AppCertificate:
    """PEM fourni par l'exploitant : cle privee et certificat dans le meme fichier."""
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        key = serialization.load_pem_private_key(raw, password=password.encode() or None)
    except (TypeError, ValueError) as exc:
        raise CertificateError(
            f"Cle privee illisible dans {path} (mot de passe MS_CERT_PASSWORD ?) : {exc}"
        ) from exc
    try:
        cert = x509.load_pem_x509_certificate(raw)
    except ValueError as exc:
        raise CertificateError(f"Certificat absent de {path} : {exc}") from exc
    return AppCertificate(key=key, cert=cert, source="fourni")


def _generate() -> AppCertificate:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EZ365 SharePoint")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=VALIDITY_DAYS))
        .sign(key, hashes.SHA256())
    )

    os.makedirs(get_settings().data_dir, exist_ok=True)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(_key_password()),
    )
    key_path = _data_path(KEY_FILE)
    with open(key_path, "wb") as handle:
        handle.write(key_pem)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    with open(_data_path(CERT_FILE), "wb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))

    log.info("Certificat SharePoint genere (empreinte %s).",
             cert.fingerprint(hashes.SHA1()).hex().upper())
    return AppCertificate(key=key, cert=cert, source="genere")


def _load_generated() -> AppCertificate | None:
    key_path, cert_path = _data_path(KEY_FILE), _data_path(CERT_FILE)
    if not (os.path.exists(key_path) and os.path.exists(cert_path)):
        return None
    with open(key_path, "rb") as handle:
        try:
            key = serialization.load_pem_private_key(handle.read(), password=_key_password())
        except (TypeError, ValueError) as exc:
            raise CertificateError(
                "Cle privee du certificat illisible : STORAGE_KEY a-t-elle change ? "
                "Supprimez les fichiers sharepoint-app.* de /data pour en generer "
                "un nouveau, puis deposez-le dans Azure."
            ) from exc
    with open(cert_path, "rb") as handle:
        cert = x509.load_pem_x509_certificate(handle.read())
    return AppCertificate(key=key, cert=cert, source="genere")


def load(create: bool = True) -> AppCertificate | None:
    """Certificat a utiliser ; le genere au besoin si `create`."""
    settings = get_settings()
    with _lock:
        if settings.ms_cert_path:
            return _load_external(settings.ms_cert_path, settings.ms_cert_password)
        current = _load_generated()
        if current is None and create:
            current = _generate()
        return current


def regenerate() -> AppCertificate:
    """Remplace le certificat genere (a redeposer ensuite dans Azure)."""
    if get_settings().ms_cert_path:
        raise CertificateError(
            "Le certificat vient de MS_CERT_PATH : remplacez ce fichier plutot."
        )
    with _lock:
        for name in (KEY_FILE, CERT_FILE):
            try:
                os.remove(_data_path(name))
            except FileNotFoundError:
                pass
        return _generate()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def client_assertion(certificate: AppCertificate, token_url: str, client_id: str) -> str:
    """Assertion JWT signee RS256, a joindre a une demande de jeton."""
    now = int(time.time())
    header = {
        "alg": "RS256",
        "typ": "JWT",
        "x5t": _b64url(certificate.cert.fingerprint(hashes.SHA1())),
    }
    claims = {
        "aud": token_url,
        "iss": client_id,
        "sub": client_id,
        "jti": str(uuid.uuid4()),
        "nbf": now - 30,
        "iat": now,
        "exp": now + 600,
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in (header, claims)
    )
    signature = certificate.key.sign(
        signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    return f"{signing_input}.{_b64url(signature)}"
