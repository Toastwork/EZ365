#!/bin/sh
# Connecte la CLI Bitwarden au serveur, deverrouille le coffre, puis publie
# la Vault Management API sur 0.0.0.0:8087 pour le conteneur ez365.
#
# Le dossier /data est persistant : au 2e demarrage une session existe deja.
# Deux pieges de la CLI en decoulent, et dictent la logique ci-dessous :
#   - « bw config server » refuse de s'executer tant qu'une session existe
#     (« Logout required before server config update. ») ;
#   - « bw login --check » renvoie un code non nul quand le coffre est
#     verrouille, alors que le compte EST authentifie.
# On se fie donc au champ `status` de « bw status », seul indicateur fiable :
#   unauthenticated -> il faut se connecter
#   locked / unlocked -> deja connecte, il ne reste qu'a deverrouiller.
#
# Regle generale : aucune commande ne doit echouer en silence. Chaque echec
# journalise la sortie reelle de la CLI avant d'arreter le conteneur, sinon le
# redemarrage automatique masque la cause.
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

fatal() {
  log "$1"
  if [ $# -gt 1 ] && [ -n "$2" ]; then
    log "Detail CLI : $2"
  fi
  exit 1
}

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
  fatal "Configuration du serveur impossible. Videz le volume /data de ce conteneur (ez365-bw-cli) puis relancez-le." "$output"
}

# Authentifie le compte de service par cle API (BW_CLIENTID / BW_CLIENTSECRET,
# lus directement dans l'environnement par la CLI).
authenticate() {
  if output="$(bw login --apikey 2>&1)"; then
    return 0
  fi
  case "$output" in
    *"already logged in"*|*"You are logged in"*)
      log "La CLI signale une session deja ouverte, on continue."
      return 0 ;;
  esac
  fatal "Authentification refusee. Verifiez BW_CLIENTID et BW_CLIENTSECRET (cle API du compte de service Bitwarden)." "$output"
}

# ---------------------------------------------------------------------------
# 1. Serveur
# ---------------------------------------------------------------------------
current_status="$(bw_status_field status)"
current_server="$(bw_status_field serverUrl)"
current_server="${current_server%/}"

log "Serveur souhaite : $BW_SERVER"
log "Etat au demarrage : ${current_status:-inconnu} (serveur : ${current_server:-aucun})"

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

# ---------------------------------------------------------------------------
# 2. Authentification
# ---------------------------------------------------------------------------
# Relu apres l'etape 1 : une deconnexion a pu changer l'etat.
current_status="$(bw_status_field status)"

case "$current_status" in
  locked|unlocked)
    log "Compte deja authentifie (coffre $current_status)."
    ;;
  *)
    log "Authentification par cle API…"
    authenticate
    ;;
esac

# ---------------------------------------------------------------------------
# 3. Deverrouillage
# ---------------------------------------------------------------------------
# `bw status` peut annoncer « locked » alors que les jetons de la session
# stockee dans /data sont perimes : le deverrouillage repond alors « You are
# not logged in. ». Les jetons d'une connexion par cle API expirant avec le
# temps, cet etat revient tot ou tard — on se reauthentifie sur place plutot
# que d'exiger une purge manuelle du volume.
log "Deverrouillage…"
if ! BW_SESSION="$(bw unlock --passwordenv BW_PASSWORD --raw 2>&1)"; then
  case "$BW_SESSION" in
    *"not logged in"*)
      log "Session locale perimee malgre l'etat « $current_status » : reauthentification."
      bw logout >/dev/null 2>&1 || true
      set_server
      authenticate
      if ! BW_SESSION="$(bw unlock --passwordenv BW_PASSWORD --raw 2>&1)"; then
        fatal "Deverrouillage impossible apres reauthentification. Verifiez BW_PASSWORD (mot de passe maitre du compte de service)." "$BW_SESSION"
      fi
      log "Reauthentification reussie." ;;
    *)
      fatal "Deverrouillage impossible. Verifiez BW_PASSWORD (mot de passe maitre du compte de service)." "$BW_SESSION" ;;
  esac
fi
[ -n "$BW_SESSION" ] || fatal "Deverrouillage sans cle de session : verifiez BW_PASSWORD."
export BW_SESSION

# Une synchronisation qui echoue n'empeche pas de servir : on avertit seulement.
log "Synchronisation du coffre…"
if ! output="$(bw sync 2>&1)"; then
  log "Synchronisation echouee (le coffre reste utilisable) : $output"
fi

log "Coffre deverrouille, demarrage de l'API sur 0.0.0.0:$PORT"

# Le mot de passe maitre n'est plus necessaire une fois la session ouverte.
unset BW_PASSWORD BW_CLIENTSECRET

exec bw serve --hostname 0.0.0.0 --port "$PORT"
