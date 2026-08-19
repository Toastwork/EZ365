"""Chiffrement au repos des secrets stockes en base (STORAGE_KEY).

STORAGE_KEY est une cle Fernet (32 octets encodes en base64 url-safe), telle
que celle deja presente dans le compose. Si la valeur fournie n'est pas une
cle Fernet valide, on la derive en SHA-256 pour rester tolerant.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    raw = get_settings().storage_key
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
        return Fernet(derived)


def encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith(_PREFIX):
        # Valeur ecrite avant l'activation du chiffrement : on la rend telle quelle.
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        raise RuntimeError(
            "Dechiffrement impossible : STORAGE_KEY ne correspond pas aux donnees de /data"
        )


def session_secret() -> str:
    """Cle de signature des cookies de session, derivee de STORAGE_KEY."""
    return hashlib.sha256(("session|" + get_settings().storage_key).encode()).hexdigest()
