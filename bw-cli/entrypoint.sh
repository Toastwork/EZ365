#!/bin/sh
# Connecte la CLI Bitwarden au serveur, deverrouille le coffre, puis publie
# la Vault Management API sur 0.0.0.0:8087 pour le conteneur ez365.
#
# Le dossier /data est persistant : au 2e demarrage une session existe deja.
# La CLI refuse alors « bw config server » avec « Logout required before server
# config update. » — on ne reconfigure donc le serveur que si c'est necessaire.
set -eu

: "${BW_SERVER:?BW_SERVER est requis}"
: "${BW_CLIENTID:?BW_CLIENTID est requis}"
: "${BW_CLIENTSECRET:?BW_CLIENTSECRET est requis}"
: "${BW_PASSWORD:?BW_PASSWORD est requis}"

PORT="${BW_PORT:-8087}"
export BITWARDENCLI_APPDATA_DIR="${BITWARDENCLI_APPDATA_DIR:-/data}"
mkdir -p "$BITWARDENCLI_APPDATA_DIR"

# La CLI stocke l'URL sans barre oblique finale : on normalise pour comparer.
BW_SERVER="${BW_SERVER%/}"

log() { echo "[bw-cli] $*"; }

# Extrait un champ de `bw status`. La sortie est un objet JSON plat, parfois
# precede d'avertissements : on decoupe sur les virgules et on prend la
# premiere correspondance. `null` est ramene a une chaine vide.
bw_status_field() {
  value="$(
    bw status 2>/dev/null \
      | tr ',' '\n' \
      | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" \
      | head -n 1
  )"
  [ "$value" = "null" ] && value=""
  printf '%s' "$value"
}

current_status="$(bw_status_field status)"
current_server="$(bw_status_field serverUrl)"
current_server="${current_server%/}"

log "Serveur souhaite : $BW_SERVER"
log "Etat au demarrage : ${current_status:-inconnu} (serveur : ${current_server:-aucun})"

# Configure le serveur, en se deconnectant d'abord si la CLI l'exige.
set_server() {
  if output="$(bw config server "$BW_SERVER" 2>&1)"; then
    return 0
  fi
  log "« bw config server » a echoue : $output"
  log "Nouvelle tentative apres deconnexion de la session locale."
  bw logout >/dev/null 2>&1 || true
  if output="$(bw config server "$BW_SERVER" 2>&1)"; then
    return 0
  fi
  log "Configuration du serveur impossible : $output"
  log "Videz le volume /data de ce conteneur (ez365-bw-cli) puis relancez-le."
  return 1
}

if [ -z "$current_status" ] || [ "$current_status" = "unauthenticated" ]; then
  log "Aucune session : configuration du serveur."
  set_server
elif [ "$current_server" != "$BW_SERVER" ]; then
  log "Serveur different de la session existante : deconnexion puis reconfiguration."
  bw logout >/dev/null 2>&1 || true
  set_server
else
  log "Serveur deja configure, session existante conservee."
fi

# `bw login --apikey` lit BW_CLIENTID / BW_CLIENTSECRET dans l'environnement.
if bw login --check >/dev/null 2>&1; then
  log "Deja authentifie."
else
  log "Authentification par cle API…"
  bw login --apikey --quiet
fi

log "Deverrouillage…"
BW_SESSION="$(bw unlock --passwordenv BW_PASSWORD --raw)"
export BW_SESSION
[ -n "$BW_SESSION" ] || { log "Echec du deverrouillage : verifiez BW_PASSWORD."; exit 1; }

log "Synchronisation du coffre…"
bw sync >/dev/null

log "Coffre deverrouille, demarrage de l'API sur 0.0.0.0:$PORT"

# Le mot de passe maitre n'est plus necessaire une fois la session ouverte.
unset BW_PASSWORD BW_CLIENTSECRET

exec bw serve --hostname 0.0.0.0 --port "$PORT"
