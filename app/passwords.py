"""Generation de mots de passe initiaux conformes aux exigences Entra ID."""
from __future__ import annotations

import secrets

# Caracteres sans ambiguite visuelle (pas de O/0, l/1/I) : ces mots de passe
# sont parfois lus au telephone lors d'une remise en main propre.
LOWER = "abcdefghijkmnopqrstuvwxyz"
UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
DIGITS = "23456789"
SYMBOLS = "!@#$%*+-=?"
ALL = LOWER + UPPER + DIGITS + SYMBOLS


def generate(length: int = 16) -> str:
    length = max(12, min(length, 64))
    while True:
        chars = [
            secrets.choice(LOWER),
            secrets.choice(UPPER),
            secrets.choice(DIGITS),
            secrets.choice(SYMBOLS),
        ]
        chars += [secrets.choice(ALL) for _ in range(length - len(chars))]
        secrets.SystemRandom().shuffle(chars)
        candidate = "".join(chars)
        if is_acceptable(candidate):
            return candidate


def is_acceptable(value: str) -> bool:
    """Entra ID exige 8 a 256 caracteres et 3 des 4 familles."""
    if not 8 <= len(value) <= 256:
        return False
    families = sum(
        [
            any(c.islower() for c in value),
            any(c.isupper() for c in value),
            any(c.isdigit() for c in value),
            any(not c.isalnum() for c in value),
        ]
    )
    return families >= 3
