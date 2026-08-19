#!/bin/sh
# Connecte la CLI Bitwarden au serveur, deverrouille le coffre, puis publie
# la Vault Management API sur 0.0.0.0:8087 pour le conteneur ez365.
set -eu

: "${BW_SERVER:?BW_SERVER est requis}"
: "${BW_CLIENTID:?BW_CLIENTID est requis}"
: "${BW_CLIENTSECRET:?BW_CLIENTSECRET est requis}"
: "${BW_PASSWORD:?BW_PASSWORD est requis}"

PORT="${BW_PORT:-8087}"
export BITWARDENCLI_APPDATA_DIR="${BITWARDENCLI_APPDATA_DIR:-/data}"
mkdir -p "$BITWARDENCLI_APPDATA_DIR"

log() { echo "[bw-cli] $*"; }

log "Serveur : $BW_SERVER"
bw config server "$BW_SERVER" >/dev/null

# `bw login --apikey` lit BW_CLIENTID / BW_CLIENTSECRET dans l'environnement.
if bw login --check >/dev/null 2>&1; then
  log "Deja authentifie, on conserve la session."
else
  log "Authentification par cle API…"
  bw login --apikey --quiet
fi

log "Synchronisation du coffre…"
bw sync >/dev/null 2>&1 || log "Synchronisation differee (le coffre est encore verrouille)."

log "Deverrouillage…"
BW_SESSION="$(bw unlock --passwordenv BW_PASSWORD --raw)"
export BW_SESSION
[ -n "$BW_SESSION" ] || { log "Echec du deverrouillage : verifiez BW_PASSWORD."; exit 1; }

bw sync >/dev/null
log "Coffre deverrouille, demarrage de l'API sur 0.0.0.0:$PORT"

# Le mot de passe maitre n'est plus necessaire une fois la session ouverte.
unset BW_PASSWORD BW_CLIENTSECRET

exec bw serve --hostname 0.0.0.0 --port "$PORT"
