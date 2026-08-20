#!/bin/sh
# Connecte la CLI Bitwarden au serveur, deverrouille le coffre, puis publie
# la Vault Management API sur 0.0.0.0:8087 pour le conteneur ez365.
#
# Le dossier /data est persistant : au 2e demarrage une session existe deja.
# Plusieurs pieges de la CLI en decoulent, et dictent la logique ci-dessous :
#   - « bw config server » refuse de s'executer tant qu'une session existe
#     (« Logout required before server config update. ») ;
#   - « bw login --check » renvoie un code non nul quand le coffre est
#     verrouille, alors que le compte EST authentifie ;
#   - « bw logout » echoue lui aussi quand l'etat local est incoherent, sans
#     rien effacer : on supprime donc data.json nous-memes ;
#   - « bw serve » n'exploite pas BW_SESSION depuis l'environnement.
# On se fie au champ `status` de « bw status », seul indicateur fiable :
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
      | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}//p" \
      | sed 's/[",}].*//' \
      | head -n 1
  )"
  [ "$value" = "null" ] && value=""
  printf '%s' "$value"
}

# Remet la CLI a zero. `bw logout` echoue quand l'etat local est incoherent
# (la CLI se croit connectee pour refuser « config server », mais pas assez
# pour se deconnecter) : on supprime donc nous-memes data.json, qui est le seul
# endroit ou la CLI garde serveur, compte et jetons. Rien d'irremplacable n'y
# reside, le coffre vit sur le serveur.
reset_local_state() {
  log "Remise a zero de l'etat local de la CLI ($BITWARDENCLI_APPDATA_DIR)."
  bw logout >/dev/null 2>&1 || true
  rm -f "$BITWARDENCLI_APPDATA_DIR/data.json" "$BITWARDENCLI_APPDATA_DIR/data.json.lock"
}

# Configure le serveur, en repartant d'un etat propre si la CLI l'exige.
set_server() {
  if output="$(bw config server "$BW_SERVER" 2>&1)"; then
    return 0
  fi
  log "« bw config server » a echoue : $output"
  reset_local_state
  if output="$(bw config server "$BW_SERVER" 2>&1)"; then
    return 0
  fi
  fatal "Configuration du serveur impossible meme apres remise a zero. Verifiez que le volume /data de ce conteneur est accessible en ecriture." "$output"
}

# Interroge directement l'endpoint de jeton du serveur pour savoir si la cle
# API est en cause ou si le probleme est ailleurs. N'est appele qu'en cas
# d'echec : la reponse n'est journalisee que lorsqu'elle ne contient pas de
# jeton.
diagnose_credentials() {
  command -v curl >/dev/null 2>&1 || return 0
  log "Diagnostic : appel direct de $BW_SERVER/identity/connect/token"
  code="$(
    curl -sk -o /tmp/bw-diag.json -w '%{http_code}' \
      -X POST "$BW_SERVER/identity/connect/token" \
      -d grant_type=client_credentials \
      -d scope=api \
      -d "client_id=$BW_CLIENTID" \
      -d "client_secret=$BW_CLIENTSECRET" \
      -d deviceType=21 \
      -d deviceIdentifier=ez365-diagnostic \
      -d deviceName=ez365 2>/dev/null || echo 000
  )"
  case "$code" in
    200)
      log "  -> HTTP 200 : la cle API est acceptee. Le blocage vient d'ailleurs." ;;
    400|401)
      log "  -> HTTP $code : la cle API est refusee par le serveur."
      log "     Regenerez-la dans Vaultwarden (parametres du compte > cle API)"
      log "     puis mettez a jour BW_CLIENTID et BW_CLIENTSECRET dans la stack."
      log "     Reponse : $(head -c 300 /tmp/bw-diag.json 2>/dev/null)" ;;
    000)
      log "  -> $BW_SERVER injoignable depuis ce conteneur (DNS, port ou TLS)." ;;
    *)
      log "  -> HTTP $code inattendu."
      log "     Reponse : $(head -c 300 /tmp/bw-diag.json 2>/dev/null)" ;;
  esac
  rm -f /tmp/bw-diag.json
}

# Authentifie le compte de service. La cle API (BW_CLIENTID / BW_CLIENTSECRET,
# lues directement dans l'environnement par la CLI) est la methode principale ;
# si BW_EMAIL est renseigne, on retombe sur une connexion e-mail + mot de passe
# maitre, qui fonctionne tant qu'aucune double authentification n'est active.
authenticate() {
  # Juste apres une remise a zero, « bw config server » vient de reecrire
  # data.json : une connexion lancee dans la foulee peut lire un etat encore
  # incomplet et echouer sur « Account does not exist » avec des identifiants
  # pourtant valides. On reessaie donc une fois, apres une pause.
  attempt=1
  while [ "$attempt" -le 2 ]; do
    if output="$(bw login --apikey 2>&1)"; then
      [ "$attempt" -gt 1 ] && log "Authentification reussie a la 2e tentative."
      return 0
    fi
    case "$output" in
      *"already logged in"*|*"You are logged in"*)
        log "La CLI signale une session deja ouverte, on continue."
        return 0 ;;
    esac
    if [ "$attempt" -eq 1 ]; then
      log "Authentification refusee : $output"
      log "Nouvelle tentative dans 3 s (l'etat local vient d'etre reecrit)…"
      sleep 3
    fi
    attempt=$((attempt + 1))
  done

  log "Authentification par cle API refusee apres 2 tentatives : $output"

  if [ -n "${BW_EMAIL:-}" ]; then
    log "Nouvelle tentative avec BW_EMAIL ($BW_EMAIL) et le mot de passe maitre…"
    if fallback="$(bw login "$BW_EMAIL" --passwordenv BW_PASSWORD 2>&1)"; then
      log "Authentification par e-mail reussie."
      return 0
    fi
    log "Authentification par e-mail refusee : $fallback"
  fi

  case "$output" in
    *"Account does not exist"*)
      log "« Account does not exist » : le serveur ne connait pas ce compte."
      log "Causes usuelles — cle API d'un autre serveur Vaultwarden, compte"
      log "supprime/recree, ou BW_SERVER qui ne pointe pas sur l'instance"
      log "hebergeant le compte de service."
      [ -n "${BW_EMAIL:-}" ] || log "Astuce : renseignez BW_EMAIL pour tenter une connexion e-mail + mot de passe." ;;
  esac

  diagnose_credentials
  fatal "Authentification impossible aupres de $BW_SERVER." "$output"
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
      # La purge est indispensable : sans elle, « config server » et
      # « login --apikey » se heurtent tous deux a la session fantome.
      reset_local_state
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

# Verification differee : l'API peut demarrer tout en annoncant un coffre
# verrouille. Le signaler ici evite d'avoir a le deduire du /healthz d'ez365.
if command -v curl >/dev/null 2>&1; then
  (
    sleep 5
    # Extraction sans retro-reference : on coupe avant, puis apres la valeur.
    served="$(curl -s "http://127.0.0.1:$PORT/status" 2>/dev/null \
      | sed -n 's/.*"status"[[:space:]]*:[[:space:]]*"//p' \
      | sed 's/".*//' \
      | head -n 1)"
    case "$served" in
      unlocked) log "Verification : API en ligne, coffre deverrouille." ;;
      "")       log "Verification : aucune reponse sur le port $PORT." ;;
      *)        log "Verification : l'API annonce un coffre « $served » — ez365 la considerera indisponible." ;;
    esac
  ) &
fi

# `bw serve` n'exploite pas BW_SESSION depuis l'environnement : sans --session
# l'API demarre mais annonce un coffre « locked », et ez365 la considere
# indisponible. On passe donc la cle explicitement.
exec bw serve --hostname 0.0.0.0 --port "$PORT" --session "$BW_SESSION"
