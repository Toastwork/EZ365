// Deploiement de raccourcis : un site, des dossiers pour tous, puis au cas par cas.

const deploy = {
  folders: [],   // dossiers du site choisi : {name, path, level}
  users: [],     // comptes eligibles : {displayName, userPrincipalName, licenses}
};

function deployForm() {
  return document.getElementById("deploy");
}

function deployApi(path) {
  return "/api/tenants/" + encodeURIComponent(deployForm().dataset.tenant) + path;
}

function readJson(field) {
  try { return JSON.parse(field.value || "[]"); } catch (err) { return []; }
}

function filterList(selector, term) {
  const needle = (term || "").trim().toLowerCase();
  document.querySelectorAll(selector).forEach(function (item) {
    item.classList.toggle("hidden", !!needle && item.textContent.toLowerCase().indexOf(needle) === -1);
  });
}

function folderLabel(path) {
  return path ? path : "Bibliotheque entiere";
}

// ---------------------------------------------------------------------------
// 1. Site
// ---------------------------------------------------------------------------
function pickSite(radio) {
  document.querySelectorAll("#site-list .site-item").forEach(function (item) {
    item.classList.toggle("selected", item.contains(radio));
  });
  deployForm().querySelector("[name=site_name]").value = radio.dataset.name || "";
  loadSiteFolders(radio.value);
}

async function findSiteByUrl() {
  const field = document.getElementById("deploy-site-url");
  const status = document.getElementById("deploy-site-status");
  const url = (field.value || "").trim();
  if (!url) { return; }
  status.textContent = "Recherche du site…";
  try {
    const resp = await fetch(deployApi("/resolve-site?url=" + encodeURIComponent(url)),
      { headers: { Accept: "application/json" } });
    const data = await resp.json();
    if (!resp.ok) { status.textContent = data.error || "Site introuvable."; return; }

    const list = document.getElementById("site-list");
    let radio = Array.from(list.querySelectorAll("input[name=site_id]"))
      .find(function (r) { return r.value === data.id; });
    if (!radio) {
      const item = document.createElement("label");
      item.className = "site-item";
      item.innerHTML = '<input type="radio" name="site_id" onchange="pickSite(this)">' +
                       '<span class="site-name"></span><span class="site-url muted small"></span>';
      radio = item.querySelector("input");
      radio.value = data.id;
      radio.dataset.name = data.displayName || "";
      item.querySelector(".site-name").textContent = data.displayName || "Site";
      item.querySelector(".site-url").textContent = data.webUrl || "";
      list.prepend(item);
    }
    radio.checked = true;
    pickSite(radio);
    field.value = "";
    status.textContent = "Site retrouve et selectionne.";
  } catch (err) {
    status.textContent = "Recherche impossible.";
  }
}

async function loadSiteFolders(siteId) {
  const state = document.getElementById("folder-state");
  const tree = document.getElementById("mass-folders");
  deploy.folders = [];
  tree.innerHTML = "";
  setMassFolders([]);
  state.textContent = "Lecture des dossiers…";
  try {
    const resp = await fetch(deployApi("/folders?depth=3&site_id=" + encodeURIComponent(siteId)),
      { headers: { Accept: "application/json" } });
    const data = await resp.json();
    if (!resp.ok) { state.textContent = data.error || "Dossiers illisibles."; renderFolderSelects(); return; }
    deploy.folders = data.folders || [];
    const library = (data.drive && data.drive.name) || "Documents";
    state.textContent = "Cochez les dossiers de « " + library + " » a poser chez tout le monde.";
  } catch (err) {
    state.textContent = "Lecture des dossiers impossible.";
  }
  renderFolderTree();
  renderFolderSelects();
}

// ---------------------------------------------------------------------------
// 2. Pour tous
// ---------------------------------------------------------------------------
function massFolders() {
  return readJson(deployForm().querySelector("[name=mass_folders]"));
}

function setMassFolders(list) {
  deployForm().querySelector("[name=mass_folders]").value = JSON.stringify(list);
  refreshDeployInfo();
}

function renderFolderTree() {
  const tree = document.getElementById("mass-folders");
  tree.innerHTML = "";
  const entries = [{ name: "Bibliotheque entiere", path: "", level: 0 }].concat(deploy.folders);
  entries.forEach(function (folder) {
    const row = document.createElement("label");
    row.className = "check tree-item";
    row.style.paddingLeft = (folder.level * 1.3) + "rem";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = folder.path;
    box.onchange = function () {
      const list = massFolders().filter(function (p) { return p !== folder.path; });
      if (box.checked) { list.push(folder.path); }
      setMassFolders(list);
    };
    const name = document.createElement("span");
    name.textContent = folder.name;
    if (!folder.path) { name.className = "muted"; }
    row.appendChild(box);
    row.appendChild(name);
    tree.appendChild(row);
  });
}

function excludedUpns() {
  return Array.from(document.querySelectorAll("#exclude-list input:checked"))
    .map(function (box) { return box.value; });
}

// ---------------------------------------------------------------------------
// 3. Par utilisateur
// ---------------------------------------------------------------------------
async function loadDeployUsers() {
  const state = document.getElementById("users-state");
  try {
    const resp = await fetch(deployApi("/users?deployable=1"), { headers: { Accept: "application/json" } });
    const data = await resp.json();
    if (!resp.ok) { state.textContent = data.error || "Utilisateurs illisibles."; return; }
    deploy.users = data.users || [];
  } catch (err) {
    state.textContent = "Lecture des utilisateurs impossible.";
    return;
  }
  state.textContent = deploy.users.length
    ? "Ajoutez un dossier a un compte pour le lui deployer en plus."
    : "Aucun compte actif avec licence sur ce tenant.";
  renderUsers();
  refreshDeployInfo();
}

function renderUsers() {
  const rows = document.getElementById("user-rows");
  const excludes = document.getElementById("exclude-list");
  rows.innerHTML = "";
  excludes.innerHTML = "";
  deploy.users.forEach(function (user) {
    const upn = (user.userPrincipalName || "").toLowerCase();
    const name = user.displayName || upn;

    const ex = document.createElement("label");
    ex.className = "check";
    ex.innerHTML = '<input type="checkbox" name="exclude_upn" onchange="refreshDeployInfo()"><span></span>';
    ex.querySelector("input").value = upn;
    ex.querySelector("span").textContent = name + " — " + upn;
    excludes.appendChild(ex);

    const row = document.createElement("div");
    row.className = "user-row";
    row.dataset.upn = upn;
    row.innerHTML =
      '<div class="user-id"><strong></strong><span class="muted mono small"></span>' +
      '<span class="muted small user-mass"></span></div>' +
      '<div class="user-folders">' +
      '<input type="hidden" name="deploy_upn"><input type="hidden" name="deploy_name">' +
      '<input type="hidden" name="deploy_folders" value="[]">' +
      '<div class="chips"></div>' +
      '<select class="deploy-folder-select" onchange="addUserFolder(this)"></select></div>';
    row.querySelector("strong").textContent = name;
    row.querySelector(".user-id .mono").textContent = upn;
    row.querySelector("[name=deploy_upn]").value = upn;
    row.querySelector("[name=deploy_name]").value = name;
    rows.appendChild(row);
  });
  renderFolderSelects();
}

function renderFolderSelects() {
  document.querySelectorAll(".deploy-folder-select").forEach(function (select) {
    select.innerHTML = "";
    const ready = siteChosen();
    select.appendChild(new Option(ready ? "+ ajouter un dossier…" : "choisissez d'abord un site", ""));
    select.disabled = !ready;
    if (!ready) { return; }
    select.appendChild(new Option("Bibliotheque entiere", "__root__"));
    deploy.folders.forEach(function (folder) {
      const option = new Option("  ".repeat(folder.level - 1) + folder.name, folder.path);
      option.title = folder.path;
      select.appendChild(option);
    });
  });
}

function siteChosen() {
  return !!deployForm().querySelector("input[name=site_id]:checked");
}

function addUserFolder(select) {
  const value = select.value;
  select.value = "";
  if (!value) { return; }
  const folder = value === "__root__" ? "" : value;
  const row = select.closest(".user-row");
  const field = row.querySelector("[name=deploy_folders]");
  const list = readJson(field);
  if (list.indexOf(folder) === -1) { list.push(folder); }
  field.value = JSON.stringify(list);
  renderUserChips(row);
  refreshDeployInfo();
}

function renderUserChips(row) {
  const field = row.querySelector("[name=deploy_folders]");
  const chips = row.querySelector(".chips");
  chips.innerHTML = "";
  readJson(field).forEach(function (folder, index) {
    const chip = document.createElement("span");
    chip.className = "chip";
    const label = document.createElement("span");
    label.textContent = folderLabel(folder);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.onclick = function () {
      const list = readJson(field);
      list.splice(index, 1);
      field.value = JSON.stringify(list);
      renderUserChips(row);
      refreshDeployInfo();
    };
    chip.appendChild(label);
    chip.appendChild(remove);
    chips.appendChild(chip);
  });
  row.classList.toggle("has-folders", readJson(field).length > 0);
}

function customisedRows() {
  return Array.from(document.querySelectorAll("#user-rows .user-row"))
    .filter(function (row) { return readJson(row.querySelector("[name=deploy_folders]")).length; });
}

// ---------------------------------------------------------------------------
// Resumes
// ---------------------------------------------------------------------------
function refreshDeployInfo() {
  const mass = massFolders();
  const excluded = excludedUpns();
  const reached = deploy.users.length - excluded.length;
  const massText = mass.map(folderLabel).join(", ");

  document.getElementById("mass-info").textContent = mass.length
    ? mass.length + " dossier(s) → " + reached + " utilisateur(s)"
    : deploy.users.length + " utilisateur(s) eligible(s)";
  document.getElementById("exclude-info").textContent = excluded.length
    ? "(" + excluded.length + " exclu(s))" : "";

  document.querySelectorAll("#user-rows .user-row").forEach(function (row) {
    const out = excluded.indexOf(row.dataset.upn) !== -1;
    row.querySelector(".user-mass").textContent = mass.length
      ? (out ? "exclu du deploiement commun" : "recoit aussi : " + massText)
      : "";
  });
  const custom = customisedRows().length;
  document.getElementById("per-user-info").textContent = custom ? custom + " personnalise(s)" : "";
}

function confirmDeploy(form) {
  if (!siteChosen()) {
    alert("Choisissez d'abord un site.");
    return false;
  }
  const mass = massFolders();
  const custom = customisedRows();
  if (!mass.length && !custom.length) {
    alert("Cochez au moins un dossier, pour tous ou pour un utilisateur.");
    return false;
  }
  const reached = deploy.users.length - excludedUpns().length;
  const site = form.querySelector("[name=site_name]").value || "le site choisi";
  let message = "Site : " + site + "\n";
  if (mass.length) {
    message += "\nPour tous (" + reached + " utilisateur(s)) : " + mass.map(folderLabel).join(", ");
  }
  if (custom.length) {
    message += "\nAjouts individuels : " + custom.length + " utilisateur(s)";
  }
  message += "\n\nLes OneDrive manquants seront crees et les utilisateurs ajoutes " +
             "au site s'il s'agit d'un site d'equipe. Continuer ?";
  if (!confirm(message)) { return false; }

  // Seules les lignes personnalisees partent : les trois champs d'une ligne
  // sont desactives ensemble pour garder l'alignement.
  document.querySelectorAll("#user-rows .user-row").forEach(function (row) {
    const keep = custom.indexOf(row) !== -1;
    row.querySelectorAll("input[type=hidden]").forEach(function (i) { i.disabled = !keep; });
  });
  return true;
}

document.addEventListener("DOMContentLoaded", function () {
  if (!deployForm()) { return; }
  loadDeployUsers();
  const only = document.querySelectorAll("#site-list input[name=site_id]");
  if (only.length === 1) { only[0].checked = true; pickSite(only[0]); }
});
