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
  const row = body.rows[0].cloneNode(true);
  row.querySelectorAll("input").forEach(function (input) {
    input.value = "";
    delete input.dataset.touched;
  });
  body.appendChild(row);
  row.querySelector("input").focus();
}

function removeRow(button) {
  const body = document.getElementById("users-body");
  if (body.rows.length > 1) {
    button.closest("tr").remove();
  } else {
    button.closest("tr").querySelectorAll("input").forEach(function (i) { i.value = ""; });
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
  const total = rows + bulk;
  if (total === 0) {
    alert("Ajoutez au moins un utilisateur.");
    return false;
  }
  const skus = form.querySelectorAll("input[name=sku_id]:checked").length;
  let message = "Creer " + total + " utilisateur(s)";
  message += skus ? " avec " + skus + " licence(s)" : " SANS licence (pas de OneDrive possible)";
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
