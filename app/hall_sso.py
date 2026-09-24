"""Connexion unique depuis HALL.

HALL authentifie l'operateur sur l'annuaire, puis lui remet, pour chaque
application ouverte depuis le carrousel, un jeton signe Ed25519 valable
60 secondes et a usage unique. Ce module le verifie.

Il ne connait que la cle PUBLIQUE de HALL (HALL_PUBLIC_KEY) : une application
compromise ne peut pas fabriquer de jeton pour les autres. Sans cette
variable, la connexion par HALL est simplement desactivee et le formulaire
de connexion habituel reste le seul chemin.

Fichier identique dans chaque application ; la reference est
HALL/sso_client/hall_sso.py. Ne depend que de « cryptography ».
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Toutes les applications tournent sur la meme Debian que HALL : l'ecart
# d'horloge est nul en pratique, la tolerance couvre un poste lent a poster.
TOLERANCE_S = 30


class JetonInvalide(Exception):
    """Message affichable : jamais de detail cryptographique dedans."""


def _b64d(texte: str) -> bytes:
    return base64.urlsafe_b64decode(texte + "=" * (-len(texte) % 4))


def cle_publique() -> str:
    return os.getenv("HALL_PUBLIC_KEY", "").strip()


def actif() -> bool:
    return bool(cle_publique())


# Jetons deja presentes, jusqu'a leur expiration. En memoire : un redemarrage
# les oublie, mais un jeton a alors de toute facon expire (60 s).
_vus: dict[str, float] = {}
_verrou = threading.Lock()


def verifier(jeton: str, audience: str, *, cle: str | None = None,
             maintenant: float | None = None) -> dict:
    """Renvoie l'identite portee par le jeton, ou leve JetonInvalide.

    Identite : sub (UPN), name, dn, groups (groupes AD, imbrication resolue).
    """
    cle = cle if cle is not None else cle_publique()
    if not cle:
        raise JetonInvalide("La connexion par HALL n'est pas configuree ici.")
    try:
        charge_b64, signature_b64 = (jeton or "").strip().split(".")
        signature = _b64d(signature_b64)
        verificateur = Ed25519PublicKey.from_public_bytes(_b64d(cle))
    except ValueError as exc:
        raise JetonInvalide("Jeton HALL illisible.") from exc
    try:
        verificateur.verify(signature, charge_b64.encode("ascii"))
    except (InvalidSignature, UnicodeEncodeError) as exc:
        raise JetonInvalide("Jeton HALL non reconnu.") from exc

    try:
        donnees = json.loads(_b64d(charge_b64))
    except ValueError as exc:
        raise JetonInvalide("Jeton HALL illisible.") from exc
    if donnees.get("iss") != "hall" or donnees.get("aud") != audience:
        # Un jeton emis pour FORTICH ne doit pas ouvrir PARAPHE.
        raise JetonInvalide("Ce jeton HALL est destine a une autre application.")
    if not donnees.get("sub") or not donnees.get("jti"):
        raise JetonInvalide("Jeton HALL incomplet.")

    t = time.time() if maintenant is None else maintenant
    expire = float(donnees.get("exp", 0))
    if expire + TOLERANCE_S < t or float(donnees.get("iat", 0)) - TOLERANCE_S > t:
        raise JetonInvalide("Jeton HALL expire : relancez l'application depuis HALL.")

    with _verrou:
        for jti, limite in list(_vus.items()):
            if limite < t:
                del _vus[jti]
        if donnees["jti"] in _vus:
            raise JetonInvalide("Jeton HALL deja utilise : relancez l'application depuis HALL.")
        _vus[donnees["jti"]] = expire + TOLERANCE_S

    donnees.setdefault("name", donnees["sub"])
    donnees.setdefault("dn", "")
    donnees.setdefault("groups", [])
    return donnees


def controler_acces(identite: dict, ou: str = "", groupe: str = "") -> None:
    """Applique les restrictions propres a l'application (LDAP_REQUIRED_OU /
    LDAP_REQUIRED_GROUP), comme le ferait son propre formulaire de connexion.

    HALL a deja resolu l'imbrication des groupes : une simple appartenance
    a la liste suffit.
    """
    if ou and not _dans_ou(identite.get("dn", ""), ou):
        raise JetonInvalide(f"Acces reserve a l'unite d'organisation « {ou} ».")
    if groupe and not any(g.lower() == groupe.lower() for g in identite.get("groups", [])):
        raise JetonInvalide(f"Acces reserve aux membres du groupe « {groupe} ».")


def _dans_ou(dn_utilisateur: str, ou: str) -> bool:
    """Meme regle que auth._dans_ou : nom court ou DN complet (sous-UO incluses)."""
    dn = dn_utilisateur.lower().replace(" ,", ",")
    cible = ou.lower().strip()
    if "=" in cible:
        return dn.endswith(cible) or f",{cible}" in dn
    return f"ou={cible}," in dn or dn.endswith(f"ou={cible}")
