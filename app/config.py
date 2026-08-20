"""Configuration lue depuis l'environnement (cf. docker-compose.yml).

Aucune variable n'est inventee : les noms correspondent exactement a ceux
declares dans le compose de production.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    # --- Authentification de l'outil -------------------------------------
    auth_mode: str = field(default_factory=lambda: _str("AUTH_MODE", "ldap").lower())
    ldap_server: str = field(default_factory=lambda: _str("LDAP_SERVER"))
    ldap_use_ssl: bool = field(default_factory=lambda: _bool("LDAP_USE_SSL", False))
    ldap_port: int = field(default_factory=lambda: int(_str("LDAP_PORT", "0") or 0))
    ldap_domain: str = field(default_factory=lambda: _str("LDAP_DOMAIN"))
    ldap_base_dn: str = field(default_factory=lambda: _str("LDAP_BASE_DN"))
    ldap_required_group: str = field(default_factory=lambda: _str("LDAP_REQUIRED_GROUP"))

    # --- Chiffrement / stockage ------------------------------------------
    storage_key: str = field(default_factory=lambda: _str("STORAGE_KEY"))
    data_dir: str = field(default_factory=lambda: _str("DATA_DIR", "/data"))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO").upper())

    # --- TLS servi directement par uvicorn --------------------------------
    ssl_certfile: str = field(default_factory=lambda: _str("SSL_CERTFILE"))
    ssl_keyfile: str = field(default_factory=lambda: _str("SSL_KEYFILE"))
    ssl_keyfile_password: str = field(default_factory=lambda: _str("SSL_KEYFILE_PASSWORD"))

    # --- Application Entra ID multi-tenant --------------------------------
    ms_client_id: str = field(default_factory=lambda: _str("MS_CLIENT_ID"))
    ms_client_secret: str = field(default_factory=lambda: _str("MS_CLIENT_SECRET"))
    ms_redirect_uri: str = field(default_factory=lambda: _str("MS_REDIRECT_URI"))

    # --- Coffre Bitwarden / Vaultwarden (sidecar bw serve) ----------------
    vault_enabled: bool = field(default_factory=lambda: _bool("VAULT_ENABLED", False))
    vault_api_url: str = field(default_factory=lambda: _str("VAULT_API_URL").rstrip("/"))

    # --- Divers -----------------------------------------------------------
    build_ref: str = field(default_factory=lambda: _str("EZ365_BUILD", "dev"))
    host: str = field(default_factory=lambda: _str("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_str("PORT", "8000")))

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "ez365.sqlite3")

    @property
    def ldap_effective_port(self) -> int:
        if self.ldap_port:
            return self.ldap_port
        return 636 if self.ldap_use_ssl else 389

    @property
    def tls_enabled(self) -> bool:
        return bool(self.ssl_certfile and self.ssl_keyfile)

    def missing(self) -> list[str]:
        """Variables obligatoires absentes, verifiees au demarrage."""
        problems: list[str] = []
        if not self.storage_key:
            problems.append("STORAGE_KEY")
        if not self.ms_client_id:
            problems.append("MS_CLIENT_ID")
        if not self.ms_client_secret:
            problems.append("MS_CLIENT_SECRET")
        if not self.ms_redirect_uri:
            problems.append("MS_REDIRECT_URI")
        if self.auth_mode == "ldap" and not self.ldap_server:
            problems.append("LDAP_SERVER")
        if self.vault_enabled and not self.vault_api_url:
            problems.append("VAULT_API_URL")
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
