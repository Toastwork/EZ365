"""Authentification des operateurs de l'outil (Active Directory / LDAP).

AUTH_MODE=ldap  : bind simple <identifiant>@LDAP_DOMAIN, puis verification
                  d'appartenance a LDAP_REQUIRED_GROUP (imbrication incluse).
AUTH_MODE=local : mode degrade pour les tests, comptes EZ365_LOCAL_USERS
                  ("alice:motdepasse,bob:autre"). A ne pas utiliser en prod.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from .config import get_settings

log = logging.getLogger(__name__)

# Regle AD "member of chain" : resout les groupes imbriques.
_IN_CHAIN = "1.2.840.113556.1.4.1941"


@dataclass
class Operator:
    username: str
    display_name: str
    groups: list[str]

    def as_session(self) -> dict:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "groups": self.groups,
        }


class AuthError(Exception):
    pass


def _escape(value: str) -> str:
    for char, repl in (("\\", r"\5c"), ("(", r"\28"), (")", r"\29"), ("*", r"\2a"), ("\0", r"\00")):
        value = value.replace(char, repl)
    return value


def _cn(dn: str) -> str:
    head = dn.split(",", 1)[0]
    return head.split("=", 1)[1] if "=" in head else head


def authenticate(username: str, password: str) -> Operator:
    settings = get_settings()
    if not username or not password:
        raise AuthError("Identifiant et mot de passe requis.")
    if settings.auth_mode == "local":
        return _authenticate_local(username, password)
    return _authenticate_ldap(username, password)


def _authenticate_local(username: str, password: str) -> Operator:
    raw = os.getenv("EZ365_LOCAL_USERS", "")
    for entry in raw.split(","):
        if ":" not in entry:
            continue
        user, _, secret = entry.partition(":")
        if user.strip() == username and secret == password:
            return Operator(username=username, display_name=username, groups=["local"])
    raise AuthError("Identifiants invalides.")


def _authenticate_ldap(username: str, password: str) -> Operator:
    from ldap3 import ALL, SIMPLE, SUBTREE, Connection, Server
    from ldap3.core.exceptions import LDAPException

    settings = get_settings()
    upn = username if "@" in username else f"{username}@{settings.ldap_domain}"

    server = Server(
        settings.ldap_server,
        port=settings.ldap_effective_port,
        use_ssl=settings.ldap_use_ssl,
        get_info=ALL,
        connect_timeout=8,
    )
    try:
        conn = Connection(
            server,
            user=upn,
            password=password,
            authentication=SIMPLE,
            auto_bind=False,
            receive_timeout=10,
        )
        if not conn.bind():
            log.info("Echec de bind LDAP pour %s : %s", upn, conn.result.get("description"))
            raise AuthError("Identifiants invalides.")
    except LDAPException as exc:
        log.error("Serveur LDAP injoignable (%s:%s) : %s",
                  settings.ldap_server, settings.ldap_effective_port, exc)
        raise AuthError("Annuaire injoignable, reessayez plus tard.") from exc

    try:
        sam = upn.split("@", 1)[0]
        conn.search(
            search_base=settings.ldap_base_dn,
            search_filter=(
                f"(&(objectClass=user)(|(userPrincipalName={_escape(upn)})"
                f"(sAMAccountName={_escape(sam)})))"
            ),
            search_scope=SUBTREE,
            attributes=["displayName", "memberOf", "distinguishedName", "userPrincipalName"],
            time_limit=10,
        )
        if not conn.entries:
            raise AuthError("Compte introuvable dans l'annuaire.")

        entry = conn.entries[0]
        user_dn = str(entry.distinguishedName)
        display = str(entry.displayName) if entry.displayName else sam
        groups = [_cn(dn) for dn in (entry.memberOf.values if entry.memberOf else [])]

        required = settings.ldap_required_group
        if required and not _is_member(conn, settings.ldap_base_dn, user_dn, required, groups):
            log.info("Acces refuse a %s : hors du groupe %s", upn, required)
            raise AuthError(f"Acces reserve aux membres du groupe « {required} ».")

        return Operator(username=upn, display_name=display, groups=groups)
    finally:
        conn.unbind()


def _is_member(conn, base_dn: str, user_dn: str, group: str, direct_groups: list[str]) -> bool:
    if any(g.lower() == group.lower() for g in direct_groups):
        return True
    # Groupes imbriques : on demande a l'AD de resoudre la chaine.
    from ldap3 import SUBTREE

    conn.search(
        search_base=base_dn,
        search_filter=(
            f"(&(objectClass=group)(cn={_escape(group)})"
            f"(member:{_IN_CHAIN}:={_escape(user_dn)}))"
        ),
        search_scope=SUBTREE,
        attributes=["cn"],
        time_limit=10,
    )
    return bool(conn.entries)


# --------------------------------------------------------------------------
# Dependances FastAPI
# --------------------------------------------------------------------------
def current_operator(request: Request) -> Operator:
    data = request.session.get("operator")
    if not data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expiree",
            headers={"X-Redirect-Login": "1"},
        )
    return Operator(
        username=data["username"],
        display_name=data.get("display_name", data["username"]),
        groups=data.get("groups", []),
    )
