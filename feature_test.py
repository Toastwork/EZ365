"""Verifications de la selection de dossier par utilisateur."""
import os, asyncio
os.environ.update({
    "AUTH_MODE": "local", "EZ365_LOCAL_USERS": "testeur:motdepasse",
    "STORAGE_KEY": "1HkwvGs9HefxF0GEAuFbvOte6qf9zLqPYnQkQpJCsJk=",
    "DATA_DIR": ".localdata", "MS_CLIENT_ID": "x", "MS_CLIENT_SECRET": "x",
    "MS_REDIRECT_URI": "http://localhost:8000/ms/callback",
    "VAULT_ENABLED": "false", "SSL_CERTFILE": "", "SSL_KEYFILE": "", "LOG_LEVEL": "WARNING",
})

from app import provisioning
from app.msgraph import sharepoint
from app.msgraph.client import GraphClient
from app.routers.provisioning import parse_bulk

fails = []
def check(label, cond, got=None):
    print(("  OK   " if cond else "  ECHEC") + f"  {label}" + ("" if cond else f"  -> {got!r}"))
    if not cond: fails.append(label)

# --- collage avec colonne dossier ----------------------------------------
rows = parse_bulk("Marie;Dupont;marie.dupont;Comptable;Finance;Comptabilite/2026\nJean;Martin;j.martin;Tech;IT")
check("6e colonne lue", rows[0]["shortcut_folders"] == ["Comptabilite/2026"], rows[0])
check("colonne absente -> aucun raccourci", rows[1]["shortcut_folders"] == [], rows[1])

u = provisioning.normalize_user(
    {"first_name": "Marie", "shortcut_folders": ["/RH/Contrats/", "Compta"]}, "c.fr", "FR")
check("dossiers normalises sans slash",
      u["shortcut_folders"] == ["RH/Contrats", "Compta"], u["shortcut_folders"])
check("un raccourci implique le OneDrive", u["provision_onedrive"] is True, u)
u2 = provisioning.normalize_user({"first_name": "Sans"}, "c.fr", "FR")
check("sans raccourci ni demande : pas de OneDrive", u2["provision_onedrive"] is False, u2)
u3 = provisioning.normalize_user({"first_name": "Seul", "provision_onedrive": True}, "c.fr", "FR")
check("OneDrive seul possible", u3["provision_onedrive"] is True and u3["shortcut_folders"] == [], u3)

# --- aplatissement de l'arborescence -------------------------------------
TREE = {
    "": [{"name": "Comptabilite", "folder": {"childCount": 2}},
         {"name": "RH", "folder": {"childCount": 0}}],
    "Comptabilite": [{"name": "2025", "folder": {"childCount": 0}},
                     {"name": "2026", "folder": {"childCount": 1}}],
    "Comptabilite/2026": [{"name": "Factures", "folder": {"childCount": 0}}],
}
class FakeGraph(GraphClient):
    def __init__(self): self.calls = []
    async def list_child_folders(self, drive_id, path=""):
        self.calls.append(path)
        return TREE.get(path, [])

async def tree_tests():
    g = FakeGraph()
    flat = await g.list_folder_tree("d1", depth=2)
    paths = [f["path"] for f in flat]
    check("profondeur 2 : petits-enfants exclus",
          paths == ["Comptabilite", "Comptabilite/2025", "Comptabilite/2026", "RH"], paths)
    check("niveaux corrects", [f["level"] for f in flat] == [1, 2, 2, 1], [f["level"] for f in flat])
    check("tri alphabetique par chemin", paths == sorted(paths, key=str.casefold), paths)

    g2 = FakeGraph()
    flat3 = await g2.list_folder_tree("d1", depth=3)
    check("profondeur 3 : petits-enfants inclus",
          "Comptabilite/2026/Factures" in [f["path"] for f in flat3], flat3)

    g3 = FakeGraph()
    capped = await g3.list_folder_tree("d1", depth=3, max_items=2)
    check("plafond respecte", len(capped) == 2, len(capped))

asyncio.run(tree_tests())

# --- raccourcis multiples par utilisateur --------------------------------
class Ctx:
    def __init__(self): self.actor = "testeur"; self.msgs = []
    def info(self, s, m, d=None): self.msgs.append(("info", m))
    def warn(self, s, m, d=None): self.msgs.append(("warn", m))
    def error(self, s, m, d=None): self.msgs.append(("error", m))
    def success(self, s, m, d=None): self.msgs.append(("success", m))

async def shortcut_tests():
    resolved, created = [], []

    async def fake_resolve(ctx, graph, site, folder):
        resolved.append(folder)
        if folder == "Inconnu":
            return None
        return {"driveId": "SITE", "itemId": "item-" + (folder or "root"), "name": "Documents"}

    async def fake_existing(graph, drive_id):
        return {"dejala"} if drive_id == "DEJA" else set()

    async def fake_add(graph, user_drive, src_drive, item, name):
        created.append((user_drive, item, name))
        return {}

    provisioning.resolve_shortcut_target = fake_resolve
    sharepoint.existing_shortcut_names = fake_existing
    sharepoint.add_shortcut = fake_add

    results = [
        {"upn": "multi@c.fr", "drive_id": "DA", "errors": [],
         "shortcut_folders": ["Compta", "RH/Contrats", ""]},
        {"upn": "meme@c.fr", "drive_id": "DB", "errors": [], "shortcut_folders": ["Compta"]},
        {"upn": "aucun@c.fr", "drive_id": "DC", "errors": [], "shortcut_folders": []},
        {"upn": "sansdrive@c.fr", "drive_id": None, "errors": [], "shortcut_folders": ["RH"]},
        {"upn": "partiel@c.fr", "drive_id": "DD", "errors": [],
         "shortcut_folders": ["Compta", "Inconnu"]},
        {"upn": "deja@c.fr", "drive_id": "DEJA", "errors": [], "shortcut_folders": ["Dejala"]},
    ]
    ctx = Ctx()
    await provisioning.add_shortcuts(ctx, None, results, {"id": "S"}, "Site client")

    check("3 raccourcis pour un meme utilisateur",
          [c for c in created if c[0] == "DA"] == [
              ("DA", "item-Compta", "Compta"),
              ("DA", "item-RH/Contrats", "Contrats"),
              ("DA", "item-root", "Site client")], [c for c in created if c[0] == "DA"])
    check("racine libellee avec le nom du site",
          results[0]["shortcuts"][2] == {"folder": "(racine)", "status": "ajoute"},
          results[0]["shortcuts"])
    check("statut agrege", results[0]["shortcut"] == "3 ajoute(s)", results[0]["shortcut"])
    check("une resolution par dossier distinct",
          resolved == ["Compta", "RH/Contrats", "", "Inconnu"], resolved)
    check("aucune resolution pour un raccourci deja present",
          "Dejala" not in resolved, resolved)
    check("aucun raccourci demande", results[2]["shortcut"] == "non demande", results[2])
    check("sans OneDrive : chaque dossier signale",
          [d["status"] for d in results[3]["shortcuts"]] == ["OneDrive absent"], results[3])
    check("echec partiel visible",
          sorted(d["status"] for d in results[4]["shortcuts"]) == ["ajoute", "cible introuvable"],
          results[4]["shortcuts"])
    check("erreur remontee pour le dossier manquant", results[4]["errors"], results[4])
    check("raccourci deja present non recree",
          results[5]["shortcut"] == "deja presents" and not [c for c in created if c[0] == "DEJA"],
          results[5])

asyncio.run(shortcut_tests())

# --- regression : le repli de slugify ne doit pas polluer les UPN ---------
from app.msgraph.sharepoint import slugify as _slug
check("slugify vide -> chaine vide", _slug("") == "", _slug(""))
check("slugify vide avec repli explicite", _slug("", fallback="site") == "site", _slug("", fallback="site"))
check("slugify ponctuation seule -> vide", _slug("***") == "", _slug("***"))

u_first = provisioning.normalize_user({"first_name": "Neo"}, "c.fr", "FR")
check("prenom seul -> upn propre", u_first["upn"] == "neo@c.fr", u_first["upn"])
u_last = provisioning.normalize_user({"last_name": "Dupont"}, "c.fr", "FR")
check("nom seul -> upn propre", u_last["upn"] == "dupont@c.fr", u_last["upn"])
u_both = provisioning.normalize_user({"first_name": "Marie", "last_name": "Dupont"}, "c.fr", "FR")
check("prenom + nom", u_both["upn"] == "marie.dupont@c.fr", u_both["upn"])

# --- comptes existants selectionnes --------------------------------------
async def existing_only_tests():
    ctx = Ctx()

    class G:
        def __init__(self, present): self.present = present; self.created = []
        async def find_user(self, upn):
            return {"id": "id-" + upn, "displayName": "Nom Annuaire", "usageLocation": "FR"}                 if upn in self.present else None
        async def create_user(self, payload):
            self.created.append(payload); return {"id": "nouveau"}
        async def update_user(self, uid, payload): pass
        async def assign_license(self, uid, skus): self.assigned.append((uid, skus))
        async def add_group_member(self, gid, uid): pass
    G.assigned = []

    spec_existing = provisioning.normalize_user(
        {"upn": "deja@c.fr", "existing_only": True, "shortcut_folders": ["RH"]}, "c.fr", "FR")
    spec_new = provisioning.normalize_user(
        {"first_name": "Neo", "sku_ids": ["sku-1"], "sku_names": ["BUSINESS"]}, "c.fr", "FR")

    check("compte existant sans licence demandee", spec_existing["sku_ids"] == [], spec_existing)
    check("licence portee par le spec", spec_new["sku_ids"] == ["sku-1"], spec_new)

    g = G(present={"deja@c.fr"})
    G.assigned = []
    res = await provisioning.create_users(ctx, g, [spec_existing, spec_new], None)
    check("compte existant non recree", bool(g.created) and g.created[0]["userPrincipalName"] == "neo@c.fr", g.created)
    check("un seul compte cree", len(g.created) == 1, g.created)
    check("licence non attribuee a l'existant",
          [a[0] for a in G.assigned] == ["nouveau"], G.assigned)
    check("nom repris de l'annuaire", res[0]["display_name"] == "Nom Annuaire", res[0])
    check("libelles de licence portes par l'entree",
          res[1]["license_names"] == ["BUSINESS"], res[1]["license_names"])
    check("existant sans mot de passe", res[0]["password"] == "", res[0])
    check("dossiers conserves", res[0]["shortcut_folders"] == ["RH"], res[0])

    # compte disparu entre la selection et le lancement
    g2 = G(present=set())
    G.assigned = []
    res2 = await provisioning.create_users(ctx, g2, [spec_existing], None)
    check("compte existant disparu -> erreur, aucune creation",
          not g2.created and res2[0]["errors"], res2[0])

    # licences accordees si l'operateur le demande
    spec_licensed = provisioning.normalize_user(
        {"upn": "deja@c.fr", "existing_only": True,
         "sku_ids": ["sku-1"], "sku_names": ["BUSINESS"]}, "c.fr", "FR")
    g3 = G(present={"deja@c.fr"})
    G.assigned = []
    await provisioning.create_users(ctx, g3, [spec_licensed], None)
    check("licence attribuee si demandee", [a[0] for a in G.assigned] == ["id-deja@c.fr"], G.assigned)

asyncio.run(existing_only_tests())

# --- nom des entrees de coffre -------------------------------------------
from app.provisioning import default_vault_name, vault_client_code
check("code client depuis le domaine", vault_client_code("acskm.fr") == "ACSKM", vault_client_code("acskm.fr"))
check("sous-domaine : 1er label", vault_client_code("Sous.Domaine.FR") == "SOUS", vault_client_code("Sous.Domaine.FR"))
check("onmicrosoft -> marqueur",
      vault_client_code("client.onmicrosoft.com") == "[CLIENT]", vault_client_code("client.onmicrosoft.com"))
check("domaine vide -> marqueur", vault_client_code("") == "[CLIENT]", vault_client_code(""))
check("nom complet",
      default_vault_name("acskm.fr", "marie.dupont@acskm.fr") == "ACSKM-OFFICE-MARIE.DUPONT",
      default_vault_name("acskm.fr", "marie.dupont@acskm.fr"))
check("nom avec marqueur",
      default_vault_name("x.onmicrosoft.com", "jean@x.onmicrosoft.com") == "[CLIENT]-OFFICE-JEAN",
      default_vault_name("x.onmicrosoft.com", "jean@x.onmicrosoft.com"))

u_vault = provisioning.normalize_user(
    {"first_name": "Marie", "vault_enabled": True, "vault_name": " ACSKM-OFFICE-MD "}, "c.fr", "FR")
check("nom du coffre conserve et nettoye", u_vault["vault_name"] == "ACSKM-OFFICE-MD", u_vault)
u_novault = provisioning.normalize_user(
    {"upn": "x@c.fr", "existing_only": True, "vault_enabled": True}, "c.fr", "FR")
check("compte existant : jamais de depot", u_novault["vault_enabled"] is False, u_novault)

# --- depot au coffre, par utilisateur ------------------------------------
async def vault_tests():
    from app.vault import bitwarden as bw
    posted = []

    async def fake_ready():
        return (True, "ok")

    async def fake_create(**kwargs):
        posted.append(kwargs)
        return {"id": "1"}

    bw.is_ready = fake_ready
    bw.create_login = fake_create

    results = [
        {"upn": "a@c.fr", "created": True, "password": "P1", "vault_enabled": True,
         "vault_name": "ACSKM-OFFICE-A", "license_names": [], "errors": []},
        {"upn": "b@c.fr", "created": True, "password": "P2", "vault_enabled": False,
         "vault_name": "", "license_names": [], "errors": []},
        {"upn": "c@c.fr", "created": False, "password": "", "vault_enabled": True,
         "vault_name": "ACSKM-OFFICE-C", "license_names": [], "errors": []},
        {"upn": "d@c.fr", "created": True, "password": "P4", "vault_enabled": True,
         "vault_name": "", "license_names": [], "errors": []},
    ]
    ctx = Ctx()
    await provisioning.store_in_vault(ctx, results, {"id": "t", "display_name": "Client"}, {})

    check("seuls les comptes demandes sont deposes",
          [p["name"] for p in posted] == ["ACSKM-OFFICE-A", "C-OFFICE-D"], [p["name"] for p in posted])
    check("depot non demande signale", results[1]["vault"] == "non demande", results[1]["vault"])
    check("compte non cree ignore", results[2]["vault"] == "ignore (compte non cree)", results[2]["vault"])
    check("nom retenu memorise", results[0]["vault_name"] == "ACSKM-OFFICE-A", results[0])
    check("mot de passe non affiche apres depot",
          results[0]["password_shown"] is False, results[0])

    # coffre indisponible : les mots de passe doivent rester visibles
    async def fake_down():
        return (False, "sidecar injoignable")
    bw.is_ready = fake_down
    down = [{"upn": "e@c.fr", "created": True, "password": "P5", "vault_enabled": True,
             "vault_name": "N", "license_names": [], "errors": []}]
    await provisioning.store_in_vault(Ctx(), down, {"id": "t"}, {})
    check("coffre indisponible : mot de passe conserve a l'affichage",
          down[0]["vault"] == "indisponible" and down[0]["password_shown"] is True, down[0])

asyncio.run(vault_tests())

print()
print("ECHECS :", fails if fails else "aucun")
raise SystemExit(1 if fails else 0)
