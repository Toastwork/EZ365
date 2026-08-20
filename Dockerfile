FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# libldap est necessaire au bind LDAP (ldap3 utilise le socket, mais openssl
# reste requis pour LDAPS et pour le TLS servi par uvicorn).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates openssl curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py .

# Renseignee par la CI (docker build --build-arg BUILD_REF=...) : elle apparait
# dans le pied de page, dans /healthz et dans les logs de demarrage, ce qui
# permet de savoir quelle version tourne reellement.
ARG BUILD_REF=dev
ENV EZ365_BUILD=$BUILD_REF

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsk "http${SSL_CERTFILE:+s}://127.0.0.1:${PORT:-8000}/healthz" || exit 1

CMD ["python", "run.py"]
