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
    // Les options marquees data-static survivent au rechargement de la liste.
    const statics = Array.from(select.querySelectorAll("option[data-static]"));
    select.innerHTML = "";
    statics.forEach(function (option) { select.appendChild(option); });
    folderCache.forEach(function (folder) {
      const option = document.createElement("option");
      option.value = folder.path;
      // Indentation visuelle des sous-dossiers.
      option.textContent = "   ".repeat(folder.level - 1) + folder.name;
      option.title = folder.path;
      select.appendChild(option);
    });
    select.value = "";
  });
}

// ---------------------------------------------------------------------------
// Raccourcis d'une ligne : plusieurs dossiers, portes par un champ JSON
// ---------------------------------------------------------------------------
function shortcutsOf(cell) {
  const hidden = cell.querySelector("input[type=hidden]");
  try { return JSON.parse(hidden.value || "[]"); } catch (err) { return []; }
}

function renderChips(cell) {
  const list = shortcutsOf(cell);
  const chips = cell.querySelector(".chips");
  chips.innerHTML = "";
  list.forEach(function (folder, index) {
    const chip = document.createElement("span");
    chip.className = "chip";
    const label = document.createElement("span");
    label.textContent = folder || "racine";
    label.title = folder || "Bibliotheque entiere";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.onclick = function () {
      const next = shortcutsOf(cell);
      next.splice(index, 1);
      cell.querySelector("input[type=hidden]").value = JSON.stringify(next);
      renderChips(cell);
    };
    chip.appendChild(label);
    chip.appendChild(remove);
    chips.appendChild(chip);
  });
}

function addShortcutChip(select) {
  const value = select.value;
  select.value = "";
  if (!value) { return; }
  const folder = value === "__root__" ? "" : value;
  const cell = select.closest(".shortcut-cell");
  const list = shortcutsOf(cell);
  if (list.indexOf(folder) !== -1) { return; }
  list.push(folder);
  cell.querySelector("input[type=hidden]").value = JSON.stringify(list);
  renderChips(cell);

  // Un raccourci suppose un OneDrive : on coche la case de la fiche.
  const card = select.closest(".person-card");
  const flag = card && card.querySelector("input[name$=_onedrive]");
  if (flag && flag.value !== "1") {
    flag.value = "1";
    const box = card.querySelector("input[type=checkbox]");
    if (box) { box.checked = true; }
  }
}

// Une case a cocher non cochee n'est pas envoyee : chacune est doublee d'un
// champ cache « 1 »/« 0 » qui, lui, garde sa place dans l'ordre des lignes.
function syncFlag(box) {
  const scope = box.closest("label") || box.parentElement;
  const hidden = scope.querySelector("input[type=hidden]");
  if (hidden) { hidden.value = box.checked ? "1" : "0"; }
}

const syncOnedrive = syncFlag;
const syncVault = syncFlag;

// ---------------------------------------------------------------------------
// Nom de l'entree Bitwarden : CLIENT-OFFICE-UTILISATEUR
// ---------------------------------------------------------------------------
function vaultClientCode(domain) {
  const value = (domain || "").trim().toLowerCase().replace(/^@/, "");
  // Le domaine technique .onmicrosoft.com ne nomme pas le client : on laisse
  // le marqueur a completer.
  if (!value || value.endsWith(".onmicrosoft.com")) { return "[CLIENT]"; }
  return value.split(".")[0].toUpperCase();
}

function refreshVaultName(card) {
  const field = card.querySelector(".vault-name");
  if (!field || field.dataset.touched) { return; }
  const aliasField = card.querySelector("input[name=alias]");
  const alias = (aliasField && aliasField.value.trim()) || "";
  const domainField = document.getElementById("user_domain");
  const domain = (domainField && domainField.value) || "";
  field.value = alias ? vaultClientCode(domain) + "-OFFICE-" + alias.toUpperCase() : "";
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
      item.innerHTML = '<span class="pick-name"></span>' +
                       '<span class="pick-upn"></span>' +
                       '<span class="pick-lic"></span>';
      item.children[0].textContent = user.displayName || user.userPrincipalName;
      item.children[1].textContent = user.userPrincipalName;
      item.children[2].textContent = (user.licenses && user.licenses.length)
        ? user.licenses.join(", ")
        : "sans licence";
      if (!user.licenses || !user.licenses.length) {
        item.children[2].classList.add("pick-lic-none");
      }
      item.onclick = function () {
        addExistingUser(user);
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

// Liste des licences du tenant, reprise du selecteur « licence par defaut »
// deja rendu par le serveur : pas besoin d'un second jeu de donnees.
function skuOptions(placeholder) {
  const select = document.createElement("select");
  select.appendChild(new Option(placeholder, ""));
  const source = document.getElementById("default_sku");
  if (source) {
    Array.from(source.options).slice(1).forEach(function (opt) {
      const copy = new Option(opt.textContent.trim(), opt.value);
      copy.disabled = opt.disabled;
      select.appendChild(copy);
    });
  }
  return select;
}

function addExistingUser(user) {
  const upn = user.userPrincipalName || "";
  const name = user.displayName || upn;
  if (pickedUpns().indexOf(upn.toLowerCase()) !== -1) { return; }

  const container = document.getElementById("existing-body");
  const card = document.createElement("div");
  card.className = "person-card existing";

  // -- en-tete : identite et retrait ---------------------------------------
  const head = document.createElement("div");
  head.className = "person-head";
  const who = document.createElement("div");
  who.innerHTML = '<span class="person-title"></span>' +
                  '<div class="muted mono small"></div>' +
                  '<input type="hidden" name="existing_upn">' +
                  '<input type="hidden" name="existing_name">';
  who.children[0].textContent = name;
  who.children[1].textContent = upn;
  who.querySelector("input[name=existing_upn]").value = upn;
  who.querySelector("input[name=existing_name]").value = name;

  const drop = document.createElement("button");
  drop.type = "button";
  drop.className = "btn btn-small";
  drop.textContent = "Retirer";
  drop.onclick = function () {
    card.remove();
    if (!container.children.length) {
      document.getElementById("existing-table").classList.add("hidden");
    }
  };
  head.appendChild(who);
  head.appendChild(drop);

  // -- licences en place ----------------------------------------------------
  const current = document.createElement("div");
  current.className = "person-licences small";
  const caption = document.createElement("span");
  caption.className = "muted";
  caption.textContent = "Licences actuelles :";
  current.appendChild(caption);
  if (user.licenses && user.licenses.length) {
    user.licenses.forEach(function (licence) {
      const pill = document.createElement("span");
      pill.className = "pill pill-ok lic";
      pill.textContent = licence;
      current.appendChild(pill);
    });
  } else {
    const pill = document.createElement("span");
    pill.className = "pill pill-warn";
    pill.textContent = "sans licence";
    current.appendChild(pill);
  }

  // -- reglages -------------------------------------------------------------
  const grid = document.createElement("div");
  grid.className = "person-grid";

  const skuBlock = document.createElement("div");
  const skuLabel = document.createElement("label");
  skuLabel.textContent = "Licence a ajouter";
  const skuSelect = skuOptions("— ne rien changer —");
  skuSelect.name = "existing_sku";
  skuBlock.appendChild(skuLabel);
  skuBlock.appendChild(skuSelect);
  grid.appendChild(skuBlock);

  const foot = document.createElement("div");
  foot.className = "person-foot";

  // OneDrive : decoche par defaut, un compte en place a souvent deja le sien.
  const driveLabel = document.createElement("label");
  driveLabel.className = "check";
  driveLabel.innerHTML =
    '<input type="hidden" name="existing_onedrive" value="0" data-default="0">' +
    '<input type="checkbox" onchange="syncOnedrive(this)"> Provisionner le OneDrive';
  foot.appendChild(driveLabel);

  const shortcuts = document.createElement("div");
  shortcuts.className = "shortcut-cell";
  shortcuts.innerHTML =
    '<label>Raccourcis vers le site</label>' +
    '<input type="hidden" name="existing_shortcuts" value="[]" data-default="[]">' +
    '<div class="chips"></div>';
  const folderSelect = document.createElement("select");
  folderSelect.className = "folder-select shortcut-add";
  folderSelect.onchange = function () { addShortcutChip(folderSelect); };
  const placeholder = new Option("+ ajouter un raccourci…", "");
  placeholder.dataset.static = "1";
  const rootOption = new Option("Bibliotheque entiere (racine)", "__root__");
  rootOption.dataset.static = "1";
  folderSelect.appendChild(placeholder);
  folderSelect.appendChild(rootOption);
  shortcuts.appendChild(folderSelect);
  foot.appendChild(shortcuts);

  const note = document.createElement("p");
  note.className = "muted small person-vault-note";
  note.textContent = "Compte existant : aucun identifiant a deposer au coffre "
                   + "(son mot de passe n'est pas connu d'EZ365).";

  [head, current, grid, foot, note].forEach(function (block) { card.appendChild(block); });
  container.appendChild(card);
  document.getElementById("existing-table").classList.remove("hidden");
  applyFolders();
}


function suggestPath(value) {
  const path = document.getElementById("site_path");
  if (!path.dataset.touched) { path.value = slug(value); }
}

function suggestAlias(input) {
  const card = input.closest(".person-card");
  const alias = card.querySelector("input[name=alias]");
  if (alias.dataset.touched) { return; }
  const first = slug(card.querySelector("input[name=first_name]").value);
  const last = slug(card.querySelector("input[name=last_name]").value);
  alias.value = [first, last].filter(Boolean).join(".");
  refreshVaultName(card);
}

function addRow() {
  const list = document.getElementById("users-list");
  // Le clone reprend la liste de dossiers deja chargee dans la 1re fiche ;
  // seules les valeurs saisies sont remises a zero.
  const card = list.firstElementChild.cloneNode(true);
  card.querySelectorAll("input:not([type=hidden]):not([type=checkbox])").forEach(function (input) {
    input.value = "";
    delete input.dataset.touched;
  });
  card.querySelectorAll("input[type=hidden]").forEach(function (hidden) {
    hidden.value = hidden.dataset.default || "";
  });
  card.querySelectorAll("input[type=checkbox]").forEach(function (box) {
    const scope = box.closest("label") || box.parentElement;
    const hidden = scope.querySelector("input[type=hidden]");
    box.checked = !hidden || hidden.value === "1";
  });
  card.querySelectorAll("select").forEach(function (select) { select.selectedIndex = 0; });
  card.querySelectorAll(".chips").forEach(function (chips) { chips.innerHTML = ""; });
  list.appendChild(card);
  renumberCards();
  refreshVaultName(card);
  card.querySelector("input:not([type=hidden])").focus();
}

// Les fiches sont numerotees pour se reperer quand la liste s'allonge.
function renumberCards() {
  const cards = document.querySelectorAll("#users-list .person-card");
  cards.forEach(function (card, index) {
    card.querySelector(".person-title").textContent =
      cards.length > 1 ? "Utilisateur " + (index + 1) : "Nouvel utilisateur";
  });
}

function removeRow(button) {
  const list = document.getElementById("users-list");
  const card = button.closest(".person-card");
  if (list.children.length > 1) {
    card.remove();
  } else {
    card.querySelectorAll("input:not([type=hidden]):not([type=checkbox])").forEach(function (i) {
      i.value = "";
    });
    card.querySelectorAll("input[type=hidden]").forEach(function (h) {
      h.value = h.dataset.default || "";
    });
    card.querySelectorAll(".chips").forEach(function (c) { c.innerHTML = ""; });
    card.querySelectorAll("select").forEach(function (sel) { sel.selectedIndex = 0; });
  }
  renumberCards();
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
  const rows = Array.from(document.querySelectorAll("#users-list .person-card"))
    .filter(function (card) {
      return Array.from(card.querySelectorAll(
        "input[name=first_name], input[name=last_name], input[name=alias]"
      )).some(function (i) { return i.value.trim(); });
    }).length;
  const bulk = (form.querySelector("[name=bulk_users]").value || "")
    .split("\n").filter(function (l) { return l.trim() && !l.startsWith("#"); }).length;
  const existing = pickedUpns().length;
  const total = rows + bulk;
  if (total === 0 && existing === 0) {
    alert("Ajoutez au moins un utilisateur.");
    return false;
  }
  const defaultSku = form.querySelector("[name=default_sku]");
  const hasDefault = defaultSku && defaultSku.value;
  const perRow = Array.from(form.querySelectorAll("[name=user_sku]"))
    .filter(function (s) { return s.value && s.value !== "none"; }).length;
  const shortcuts = Array.from(
    form.querySelectorAll("[name=user_shortcuts], [name=existing_shortcuts]")
  ).reduce(function (sum, field) {
    try { return sum + JSON.parse(field.value || "[]").length; } catch (e) { return sum; }
  }, 0);

  let message = total ? "Creer " + total + " utilisateur(s)" : "Aucun compte a creer";
  if (total) {
    message += (hasDefault || perRow)
      ? " avec licence"
      : " SANS licence (pas de OneDrive possible)";
  }
  if (existing) {
    message += ", traiter " + existing + " compte(s) deja existant(s)";
  }
  if (shortcuts) {
    message += ", poser " + shortcuts + " raccourci(s)";
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
  if (target.name === "alias" || target.id === "site_path"
      || target.classList.contains("vault-name")) {
    target.dataset.touched = "1";
  }
  if (target.name === "alias") {
    const card = target.closest(".person-card");
    if (card) { refreshVaultName(card); }
  }
});

function refreshDomain() {
  const field = document.getElementById("user_domain");
  const domain = ((field && field.value) || "").trim();
  document.querySelectorAll(".upn-domain").forEach(function (span) {
    span.textContent = domain ? "@" + domain : "@…";
  });
  document.querySelectorAll("#users-list .person-card").forEach(refreshVaultName);
}

document.addEventListener("DOMContentLoaded", function () {
  const checked = document.querySelector("input[name=site_mode]:checked");
  if (checked) { siteMode(checked.value); }
  const domain = document.getElementById("user_domain");
  if (domain) { domain.addEventListener("input", refreshDomain); }
});
