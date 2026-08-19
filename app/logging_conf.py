from __future__ import annotations

import logging
import sys

from .config import get_settings

_SENSITIVE = ("password", "secret", "clientsecret", "authorization", "client_secret")


class RedactFilter(logging.Filter):
    """Filet de securite : evite de deverser un mot de passe genere dans les logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = str(record.getMessage())
        except Exception:
            return True
        low = msg.lower()
        if any(token in low for token in _SENSITIVE) and len(msg) > 200:
            record.msg = msg[:200] + " …[tronque]"
            record.args = ()
        return True


def configure_logging() -> None:
    level = getattr(logging, get_settings().log_level, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
