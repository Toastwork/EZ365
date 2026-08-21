# EZ365

Console web d'onboarding Microsoft 365 multi-clients. Depuis une seule
interface, un technicien connecte le tenant d'un client puis, en un seul
traitement :

1. cree le site SharePoint (site d'equipe ou site de communication) ;
2. cree les comptes utilisateurs ;
3. leur attribue les licences ;
4. provisionne leur OneDrive ;
5. ajoute le raccourci du site SharePoint dans chaque OneDrive ;
6. depose les identifiants crees dans le coffre Bitwarden interne.

L'acces a l'outil est restreint aux membres d'un groupe Active Directory.

---

## Architecture

| Conteneur | Role |
|---|---|
| `ez365` | Application FastAPI (uvicorn, TLS servi directement). Interface Jinja2 + JavaScript sans dependance externe. Donnees dans `/data` (SQLite). |
| `ez365-bw-cli` | CLI Bitwarden officielle en mode `bw serve` : deverrouille le coffre au demarrage et expose la Vault Management API sur `http://bw-cli:8087`. |

`ez365` ne connait jamais le mot de passe maitre du coffre : il parle a une
session deja ouverte par le sidecar.

### Modele d'authentification Microsoft

L'application Entra ID est **multi-tenant**. Un administrateur general du
client accorde le consentement une fois (`/common/adminconsent`), puis EZ365
obtient des jetons **applicatifs** (`client_credentials`) sur le tenant du
client. Le provisionnement tourne donc en tache de fond, sans session
utilisateur et sans conserver les identifiants du client.

---

## Permissions Graph a declarer sur l'application Azure

Toutes en **permissions d'application** (pas deleguees), avec consentement
administrateur :

| Permission | Necessaire pour |
|---|---|
| `User.ReadWrite.All` | creer les comptes, definir `usageLocation` |
| `Organization.Read.All` | lire `subscribedSkus` et attribuer les licences |
| `Domain.Read.All` | detecter le domaine par defaut du tenant |
| `Group.ReadWrite.All` | creer le site d'equipe (groupe Microsoft 365) et ses membres |
| `Sites.ReadWrite.All` | lire les sites et bibliotheques |
| `Files.ReadWrite.All` | provisionner les OneDrive et y creer les raccourcis |

Pour les **sites de communication** uniquement, ajouter la permission
applicative `Sites.FullControl.All` de l'API *Office 365 SharePoint Online*
(l'endpoint `_api/SPSiteManager/create` n'accepte pas les jetons Graph). Sans
elle, le mode « site d'equipe » reste pleinement fonctionnel.

L'URI de redirection declaree sur l'application doit correspondre exactement a
`MS_REDIRECT_URI`, par exemple `https://ez365.acskm.fr:9001/ms/callback`.

---

## Variables d'environnement

Voir `.env.example`. Les valeurs attendues sont exactement celles du compose
existant — aucune variable n'a ete renommee.

| Variable | Role |
|---|---|
| `AUTH_MODE` | `ldap` (production) ou `local` (tests, via `EZ365_LOCAL_USERS`) |
| `LDAP_SERVER`, `LDAP_USE_SSL`, `LDAP_DOMAIN`, `LDAP_BASE_DN` | annuaire d'authentification des techniciens |
| `LDAP_REQUIRED_GROUP` | groupe AD exige ; les groupes imbriques sont resolus |
| `STORAGE_KEY` | cle Fernet chiffrant les donnees sensibles de `/data` et signant les sessions |
| `SSL_CERTFILE`, `SSL_KEYFILE` | TLS servi par uvicorn ; **laisser vides derriere un reverse proxy** |
| `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_REDIRECT_URI` | application Entra ID multi-tenant |
| `VAULT_ENABLED`, `VAULT_API_URL` | sidecar Bitwarden |
| `BW_SERVER`, `BW_CLIENTID`, `BW_CLIENTSECRET`, `BW_PASSWORD` | sur le conteneur `bw-cli` uniquement |
| `BW_EMAIL` | facultatif : repli e-mail + mot de passe maitre si la cle API est refusee |

Generer une cle de stockage :

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> Changer `STORAGE_KEY` apres coup rend illisibles les donnees deja chiffrees
> dans `/data`.

---

## Deploiement

Les images sont publiees sur GHCR par la CI (`.github/workflows/publish.yml`)
sous `ghcr.io/toastwork/ez365` et `ghcr.io/toastwork/ez365-bw-cli`.
Portainer les tire avec un PAT ayant le scope `read:packages`.

```bash
docker compose pull
docker compose up -d
```

Verifier l'etat du service :

```bash
curl -k https://localhost:9001/healthz
```

La reponse indique l'etat de la base, celui du coffre et les variables
d'environnement manquantes.

---

## Utilisation

1. **Se connecter** avec son compte AD (membre du groupe requis).
2. **Connecter un tenant** : ouvre la page de consentement Microsoft. Se
   connecter avec un compte administrateur general du client, ou transmettre le
   lien a l'administrateur du client (`/tenants/connect/link`). Le retour sur
   `/ms/callback` enregistre le tenant.
3. **Choisir la destination Bitwarden** du client (organisation + collection).
   Ce reglage est memorise par tenant.
4. **Provisionner** : renseigner le site, les utilisateurs (saisie ligne a
   ligne ou collage depuis Excel), les licences, puis lancer. Le journal
   s'affiche en direct et reste consultable ensuite.

### Creer le site seul

Le bouton **« Creer le site maintenant »**, dans la section SharePoint, cree le
site et son arborescence **sans toucher aux utilisateurs**. Il soumet le meme
formulaire a une autre route, qui ignore les champs utilisateur.

C'est utile quand la creation du site et l'arrivee des utilisateurs ne se font
pas le meme jour : on prepare l'espace documentaire, puis on revient plus tard
provisionner les comptes en choisissant « site existant ».

### Dossiers d'un nouveau site

Lors de la creation d'un site (equipe ou communication), un bloc « Dossiers a
creer dans Documents » permet de definir l'arborescence : un nom par ajout,
`Comptabilite/2026` creant le sous-dossier et son parent.

Ces dossiers sont crees juste apres le site, avant les utilisateurs, et
alimentent immediatement les listes de raccourcis des fiches : on peut donc
viser un dossier qui n'existe pas encore au moment de remplir le formulaire.

La creation est rejouable — un dossier deja present est traverse, pas recree —
et les noms contenant un caractere refuse par SharePoint (`" * : < > ? \ |`)
sont ecartes avec un avertissement dans le journal, sans interrompre le reste.

### Presentation

Le formulaire est decoupe en trois sections repliables :

| | Section | Contenu |
|---|---|---|
| 1 | Site SharePoint | choix ou creation du site, dossiers a creer, raccourcis communs |
| 2 | Nouveaux utilisateurs | fiches des comptes a creer, collage en masse |
| 3 | Utilisateurs deja presents | recherche et fiches des comptes existants |

Les sections 1 et 2 sont ouvertes au chargement, la 3 repliee. Chaque en-tete
porte un resume a droite — « site existant · 2 raccourcis communs », « 3 a
creer », « 1 selectionne » — qui reste lisible section fermee : on garde une
vue d'ensemble sans tout derouler.

Chaque personne occupe une **fiche** : identite et licence en haut, options de
traitement en bas. Les fiches s'empilent verticalement plutot que de s'etaler
en colonnes, un tableau devenant illisible des que chaque ligne porte licence,
OneDrive, raccourcis et coffre.

L'adresse de connexion se construit sous les yeux de l'operateur : l'alias se
deduit du prenom et du nom tant qu'on ne l'a pas saisi, et le **domaine se
choisit sur chaque fiche** parmi les domaines verifies du tenant (le domaine
par defaut vient en tete). Deux personnes d'un meme traitement peuvent donc
relever de domaines differents.

Il n'y a plus de reglage global : licence et domaine se decident fiche par
fiche. Le pays (`usageLocation`) est fixe a `FR`.

### OneDrive et raccourcis, par utilisateur

Il n'y a pas de reglage global : chaque ligne du tableau porte ses propres
options, pour maitriser exactement ce qui est applique a qui.

* **OneDrive** — une case par ligne declenche (et attend) le provisionnement.
  Cochee par defaut pour un nouveau compte, decochee pour un compte existant
  qui a souvent deja le sien.
* **Raccourcis communs** — un bloc au-dessus des fiches permet de designer un
  dossier (par exemple « Commun ») pose d'office sur **toutes** les fiches, y
  compris celles ajoutees ensuite et les comptes existants. Pour en dispenser
  quelqu'un, il suffit de retirer la pastille sur sa fiche : l'exception tient,
  meme si un autre raccourci commun est ajoute apres. Retirer la pastille du
  bloc commun, en revanche, l'enleve partout.
* **Raccourcis** — chaque personne peut en recevoir **plusieurs**. Quand un
  site existant est choisi, les dossiers de sa bibliotheque « Documents » sont
  lus et proposes (deux niveaux, 300 dossiers au maximum) ; on les ajoute un a
  un, ils s'affichent en pastilles, et « Bibliotheque entiere » vise la racine.
  Demander un raccourci coche automatiquement la case OneDrive : sans OneDrive,
  il n'y a nulle part ou le poser.

Chaque raccourci prend le nom du dossier vise, ou celui du site pour la racine.
Un raccourci deja en place est laisse tel quel, sans meme resoudre sa cible.

Les cibles sont resolues une fois par dossier distinct : dix personnes pointant
sur le meme dossier ne coutent qu'un aller-retour Graph. Le recapitulatif liste
chaque raccourci avec son resultat, ligne par ligne.

Le collage en masse accepte toujours un dossier unique en 6e colonne
(`Prenom;Nom;alias;Fonction;Service;Dossier`) ; au-dela, passer par le tableau.

### Licences

La licence se choisit **par utilisateur**, dans la colonne « Licence » du
tableau. Une ligne laissee sur « licence par defaut » prend celle definie en
tete de section ; « aucune » n'en attribue pas, meme si un defaut existe.

Chaque entree indique le nombre de postes disponibles, et une licence epuisee
n'est pas selectionnable. Une seule licence par personne : le traitement en
accepte plusieurs, mais le formulaire n'en propose qu'une.

Sans licence, pas de OneDrive — donc pas de raccourci.

### Depot dans Bitwarden

Le depot se decide **par utilisateur**, sur sa fiche, avec le nom de l'entree
modifiable. Le nom par defaut suit `CLIENT-OFFICE-UTILISATEUR` :

| Domaine du tenant | Nom propose pour `marie.dupont` |
|---|---|
| `acskm.fr` | `ACSKM-OFFICE-MARIE.DUPONT` |
| `client.onmicrosoft.com` | `[CLIENT]-OFFICE-MARIE.DUPONT` |

Un domaine en `.onmicrosoft.com` est le domaine technique du tenant : il ne
nomme pas le client, le marqueur `[CLIENT]` reste donc en clair, a completer.
Le nom se recalcule tant qu'on ne l'a pas saisi a la main, en suivant l'alias
et le domaine.

La destination (organisation et collection Bitwarden) reste reglee une fois
pour toutes sur la fiche du client, en haut de page.

Seuls les comptes **crees** peuvent etre deposes : EZ365 ne connait pas le mot
de passe d'un compte deja en place. Si le coffre est injoignable au moment du
traitement, les mots de passe generes sont affiches dans le recapitulatif pour
etre repris a la main.

### Utilisateurs deja presents sur le tenant

Le bloc « Ajouter des utilisateurs deja presents » recherche les comptes du
tenant et permet de leur attribuer un raccourci, chacun sur son dossier. Ces
comptes ne sont **jamais crees ni modifies** : pas de mot de passe genere, rien
depose au coffre. Si l'un d'eux a disparu du tenant entre la selection et le
lancement, le traitement le signale en erreur sans creer de compte a sa place.

Le tableau affiche les **licences dont ils disposent deja** (ou « sans
licence »), lues avec la recherche en un seul appel Graph. La colonne
« Licence a ajouter » reste sur « ne rien changer » par defaut : aucune licence
ne leur est attribuee tant qu'on n'en designe pas une, pour ne pas en consommer
par inadvertance. Ils disposent des memes options OneDrive et raccourcis que
les nouveaux comptes.

### Notes de fonctionnement

- **OneDrive** : le provisionnement est declenche pour tous les comptes puis
  attendu (jusqu'a 5 minutes). Une licence incluant SharePoint Online est
  indispensable. Si Microsoft n'a pas fini a temps, le traitement se termine en
  le signalant : le OneDrive se creera seul, et seul le raccourci restera a
  repasser.
- **Raccourcis** : l'API de raccourci OneDrive est capricieuse en app-only.
  L'echec est signale par utilisateur sans interrompre le reste.
- **Mots de passe** : ils ne sont affiches dans le recapitulatif **que si** le
  depot dans Bitwarden a echoue. Sinon le coffre est la seule source.
- **Comptes existants** : un UPN deja present est reutilise, jamais ecrase ;
  son mot de passe n'est pas modifie.
- **Sidecar Bitwarden** : au demarrage, si la session stockee dans `/data`
  est perimee, le conteneur purge son etat local et se reauthentifie seul.
  La premiere connexion suivant cette purge peut echouer sur « Account does
  not exist » alors que les identifiants sont bons : la CLI vient de reecrire
  son `data.json`. Une seconde tentative est faite apres une pause. Si elle
  echoue aussi, l'endpoint de jeton du serveur est interroge directement et
  les logs indiquent si la cle est refusee (HTTP 400/401) ou si le serveur
  est injoignable (000).

---

## Developpement local

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe run_local.py
```

L'application ecoute sur <http://localhost:8000> en mode `AUTH_MODE=local`
(compte `testeur` / `motdepasse`, defini par `EZ365_LOCAL_USERS`). Ce mode est
reserve aux tests.

Tests :

```bash
.venv/Scripts/python.exe smoke_test.py
.venv/Scripts/python.exe unit_test.py
```

---

## Securite

- Aucun secret n'est ecrit dans `docker-compose.yml` : ils viennent d'un `.env`
  ou des variables de la stack Portainer.
- Les mots de passe generes ne transitent que vers Entra ID et Bitwarden ; ils
  ne sont jamais journalises.
- Le `state` du flux de consentement est a usage unique et expire au bout
  d'une heure.
- Toutes les actions sensibles sont tracees dans la table `audit`.
