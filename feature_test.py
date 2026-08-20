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
check("6e colonne lue", rows[0]["shortcut_folder"] == "Comptabilite/2026", rows[0])
check("colonne absente -> vide", rows[1]["shortcut_folder"] == "", rows[1])

u = provisioning.normalize_user({"first_name": "Marie", "shortcut_folder": "/RH/Contrats/"}, "c.fr", "FR")
check("dossier normalise sans slash", u["shortcut_folder"] == "RH/Contrats", u["shortcut_folder"])

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

# --- raccourcis par utilisateur ------------------------------------------
class Ctx:
    def __init__(self): self.actor = "testeur"; self.msgs = []
    def info(self, s, m, d=None): self.msgs.append(("info", m))
    def warn(self, s, m, d=None): self.msgs.append(("warn", m))
    def error(self, s, m, d=None): self.msgs.append(("error", m))
    def success(self, s, m, d=None): self.msgs.append(("success", m))

async def shortcut_tests():
    resolved = []
    created = []

    async def fake_resolve(ctx, graph, site, folder):
        resolved.append(folder)
        if folder == "Inconnu":
            return None
        return {"driveId": "SITE", "itemId": "item-" + (folder or "root"), "name": "Documents"}

    async def fake_existing(graph, drive_id):
        return set()

    async def fake_add(graph, user_drive, src_drive, item, name):
        created.append((user_drive, item, name))
        return {}

    provisioning.resolve_shortcut_target = fake_resolve
    sharepoint.existing_shortcut_names = fake_existing
    sharepoint.add_shortcut = fake_add

    results = [
        {"upn": "a@c.fr", "drive_id": "DA", "shortcut_folder": "Comptabilite", "errors": []},
        {"upn": "b@c.fr", "drive_id": "DB", "shortcut_folder": "Comptabilite", "errors": []},
        {"upn": "c@c.fr", "drive_id": "DC", "shortcut_folder": "RH/Contrats", "errors": []},
        {"upn": "d@c.fr", "drive_id": "DD", "shortcut_folder": "", "errors": []},
        {"upn": "e@c.fr", "drive_id": None, "shortcut_folder": "RH", "errors": []},
        {"upn": "f@c.fr", "drive_id": "DF", "shortcut_folder": "Inconnu", "errors": []},
    ]
    ctx = Ctx()
    await provisioning.add_shortcuts(ctx, None, results, {"id": "S"}, "General", "Site client")

    check("une resolution par dossier distinct",
          resolved == ["Comptabilite", "RH/Contrats", "General", "Inconnu"], resolved)
    check("2 users memes dossier -> 1 seule resolution", resolved.count("Comptabilite") == 1, resolved)
    check("libelle = nom du dossier vise",
          [c[2] for c in created] == ["Comptabilite", "Comptabilite", "Contrats", "General"],
          [c[2] for c in created])
    check("cible differente par utilisateur",
          created[0][1] == "item-Comptabilite" and created[2][1] == "item-RH/Contrats", created)
    check("defaut applique si colonne vide", results[3]["shortcut_target"] == "General", results[3])
    check("sans OneDrive -> non tente", results[4]["shortcut"] == "impossible (OneDrive absent)", results[4])
    check("dossier introuvable -> signale", results[5]["shortcut"] == "cible introuvable" and results[5]["errors"], results[5])

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
        {"upn": "deja@c.fr", "existing_only": True, "shortcut_folder": "RH"}, "c.fr", "FR")
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
    check("dossier conserve", res[0]["shortcut_folder"] == "RH", res[0])

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

print()
print("ECHECS :", fails if fails else "aucun")
raise SystemExit(1 if fails else 0)
