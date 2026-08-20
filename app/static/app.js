// Interactions du formulaire de provisionnement (sans dependance externe).

function slug(value) {
  return (value || "")
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^A-Za-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
}

function siteMode(mode) {
  const isNew = mode === "team" || mode === "communication";
  document.getElementById("site-new").classList.toggle("hidden", !isNew);
  document.getElementById("site-existing").classList.toggle("hidden", mode !== "existing");
  document.getElementById("site-owner-row").classList.toggle("hidden", mode !== "communication");
  document.getElementById("site-public-row").classList.toggle("hidden", mode !== "team");

  // Les dossiers ne sont connus que pour un site deja existant : un site cree
  // a l'instant n'a pas encore de bibliotheque a explorer.
  if (mode === "existing") {
    loadFolders(document.getElementById("existing_site_id").value);
  } else {
    loadFolders("");
  }
}

// ---------------------------------------------------------------------------
// Dossiers de la bibliotheque « Documents » du site choisi
// ---------------------------------------------------------------------------
let folderCache = [];

function applyFolders() {
  document.querySelectorAll("select.folder-select").forEach(function (select) {
    const chosen = select.value;
    const placeholder = select.options[0];
    select.innerHTML = "";
    select.appendChild(placeholder);
    folderCache.forEach(function (folder) {
      const option = document.createElement("option");
      option.value = folder.path;
      // Indentation visuelle des sous-dossiers.
      option.textContent = "   ".repeat(folder.level - 1) + folder.name;
      option.title = folder.path;
      select.appendChild(option);
    });
    select.value = chosen; // conserve le choix si le dossier existe toujours
  });
}

async function loadFolders(siteId) {
  const form = document.querySelector("form.provision");
  const status = document.getElementById("folder-status");
  const setStatus = function (text) { if (status) { status.textContent = text; } };

  if (!siteId || !form) {
    folderCache = [];
    applyFolders();
    setStatus("");
    return;
  }

  setStatus("Lecture des dossiers…");
  try {
    const url = "/api/tenants/" + encodeURIComponent(form.dataset.tenant) +
                "/folders?site_id=" + encodeURIComponent(siteId);
    const resp = await fetch(url, { headers: { Accept: "application/json" } });
    const data = await resp.json();
    if (!resp.ok) {
      folderCache = [];
      applyFolders();
      setStatus(data.error || "Dossiers illisibles.");
      return;
    }
    folderCache = data.folders || [];
    applyFolders();
    const library = (data.drive && data.drive.name) || "Documents";
    setStatus(folderCache.length
      ? folderCache.length + " dossier(s) trouve(s) dans « " + library + " »."
      : "Aucun sous-dossier dans « " + library + " » : les raccourcis viseront la racine.");
  } catch (err) {
    folderCache = [];
    applyFolders();
    setStatus("Lecture des dossiers impossible.");
  }
}

// ---------------------------------------------------------------------------
// Utilisateurs deja presents sur le tenant
// ---------------------------------------------------------------------------
let searchTimer = null;

function searchExistingUsers(term) {
  // Anti-rebond : on ne veut pas une requete Graph a chaque frappe.
  clearTimeout(searchTimer);
  searchTimer = setTimeout(function () { runUserSearch(term); }, 350);
}

async function runUserSearch(term) {
  const form = document.querySelector("form.provision");
  const list = document.getElementById("user-results");
  const status = document.getElementById("user-search-status");
  if (!form || !list) { return; }

  status.textContent = "Recherche…";
  list.innerHTML = "";
  try {
    const url = "/api/tenants/" + encodeURIComponent(form.dataset.tenant) +
                "/users?q=" + encodeURIComponent(term || "");
    const resp = await fetch(url, { headers: { Accept: "application/json" } });
    const data = await resp.json();
    if (!resp.ok) { status.textContent = data.error || "Recherche impossible."; return; }

    const users = data.users || [];
    const already = pickedUpns();
    const shown = users.filter(function (u) {
      return already.indexOf((u.userPrincipalName || "").toLowerCase()) === -1;
    });
    status.textContent = shown.length
      ? shown.length + " compte(s) — cliquez pour ajouter."
      : "Aucun compte a proposer.";

    shown.forEach(function (user) {
      const item = document.createElement("li");
      item.innerHTML = '<span class="pick-name"></span><span class="pick-upn"></span>';
      item.children[0].textContent = user.displayName || user.userPrincipalName;
      item.children[1].textContent = user.userPrincipalName;
      item.onclick = function () {
        addExistingUser(user.userPrincipalName, user.displayName || user.userPrincipalName);
        item.remove();
      };
      list.appendChild(item);
    });
  } catch (err) {
    status.textContent = "Recherche impossible.";
  }
}

function pickedUpns() {
  return Array.from(document.querySelectorAll("input[name=existing_upn]"))
    .map(function (i) { return (i.value || "").toLowerCase(); });
}

function addExistingUser(upn, name) {
  if (pickedUpns().indexOf((upn || "").toLowerCase()) !== -1) { return; }
  const body = document.getElementById("existing-body");
  const row = document.createElement("tr");

  const cellUser = document.createElement("td");
  cellUser.innerHTML =
    '<span class="strong"></span><div class="muted mono small"></div>' +
    '<input type="hidden" name="existing_upn"><input type="hidden" name="existing_name">';
  cellUser.children[0].textContent = name;
  cellUser.children[1].textContent = upn;
  cellUser.querySelector("input[name=existing_upn]").value = upn;
  cellUser.querySelector("input[name=existing_name]").value = name;

  const cellFolder = document.createElement("td");
  const select = document.createElement("select");
  select.name = "existing_folder";
  select.className = "folder-select";
  select.appendChild(new Option("— dossier par defaut —", ""));
  cellFolder.appendChild(select);

  const cellAction = document.createElement("td");
  cellAction.innerHTML = '<button type="button" class="btn btn-small">×</button>';
  cellAction.firstChild.onclick = function () {
    row.remove();
    if (!body.rows.length) {
      document.getElementById("existing-table").classList.add("hidden");
    }
  };

  row.appendChild(cellUser);
  row.appendChild(cellFolder);
  row.appendChild(cellAction);
  body.appendChild(row);
  document.getElementById("existing-table").classList.remove("hidden");
  applyFolders();
}

function suggestPath(value) {
  const path = document.getElementById("site_path");
  if (!path.dataset.touched) { path.value = slug(value); }
}

function suggestAlias(input) {
  const row = input.closest("tr");
  const alias = row.querySelector("input[name=alias]");
  if (alias.dataset.touched) { return; }
  const first = slug(row.querySelector("input[name=first_name]").value);
  const last = slug(row.querySelector("input[name=last_name]").value);
  alias.value = [first, last].filter(Boolean).join(".");
}

function addRow() {
  const body = document.getElementById("users-body");
  // Le clone reprend la liste de dossiers deja chargee dans la 1re ligne ;
  // seules les valeurs saisies sont remises a zero.
  const row = body.rows[0].cloneNode(true);
  row.querySelectorAll("input").forEach(function (input) {
    input.value = "";
    delete input.dataset.touched;
  });
  row.querySelectorAll("select").forEach(function (select) {
    select.selectedIndex = 0;
  });
  body.appendChild(row);
  row.querySelector("input").focus();
}

function removeRow(button) {
  const body = document.getElementById("users-body");
  if (body.rows.length > 1) {
    button.closest("tr").remove();
  } else {
    const row = button.closest("tr");
    row.querySelectorAll("input").forEach(function (i) { i.value = ""; });
    row.querySelectorAll("select").forEach(function (s) { s.selectedIndex = 0; });
  }
}

async function reloadCollections(orgId) {
  const select = document.getElementById("vault_collection_id");
  select.innerHTML = '<option value="">— Chargement… —</option>';
  try {
    const resp = await fetch("/api/vault/collections?organization_id=" + encodeURIComponent(orgId),
      { headers: { Accept: "application/json" } });
    const data = await resp.json();
    select.innerHTML = '<option value="">— Aucune —</option>';
    (data.collections || []).forEach(function (c) {
      const opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = c.name;
      select.appendChild(opt);
    });
  } catch (err) {
    select.innerHTML = '<option value="">— Erreur de chargement —</option>';
  }
}

function confirmProvision(form) {
  const rows = Array.from(document.querySelectorAll("#users-body tr")).filter(function (tr) {
    return Array.from(tr.querySelectorAll("input")).some(function (i) { return i.value.trim(); });
  }).length;
  const bulk = (form.querySelector("[name=bulk_users]").value || "")
    .split("\n").filter(function (l) { return l.trim() && !l.startsWith("#"); }).length;
  const existing = pickedUpns().length;
  const total = rows + bulk;
  if (total === 0 && existing === 0) {
    alert("Ajoutez au moins un utilisateur.");
    return false;
  }
  const skus = form.querySelectorAll("input[name=sku_id]:checked").length;
  let message = total ? "Creer " + total + " utilisateur(s)" : "Aucun compte a creer";
  if (total) {
    message += skus ? " avec " + skus + " licence(s)" : " SANS licence (pas de OneDrive possible)";
  }
  if (existing) {
    message += ", traiter " + existing + " compte(s) deja existant(s)";
  }
  const mode = form.querySelector("input[name=site_mode]:checked").value;
  if (mode === "team" || mode === "communication") {
    message += " et creer le site « " + form.querySelector("[name=site_display_name]").value + " »";
  }
  return confirm(message + " ?\n\nCette operation modifie le tenant du client.");
}

// Un champ rempli a la main ne doit plus etre ecrase par les suggestions.
document.addEventListener("input", function (event) {
  const target = event.target;
  if (target.name === "alias" || target.id === "site_path") {
    target.dataset.touched = "1";
  }
});

document.addEventListener("DOMContentLoaded", function () {
  const checked = document.querySelector("input[name=site_mode]:checked");
  if (checked) { siteMode(checked.value); }
});
