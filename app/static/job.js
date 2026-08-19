// Suivi en direct d'un traitement : sondage du journal puis chargement du bilan.
(function () {
  const log = document.getElementById("log");
  if (!log || log.dataset.running !== "1") { return; }

  const jobId = log.dataset.job;
  const statusPill = document.getElementById("job-status");
  const waiting = document.getElementById("log-waiting");
  let lastId = 0;
  let idleTicks = 0;

  const existing = log.querySelectorAll("li");
  if (existing.length) {
    lastId = parseInt(existing[existing.length - 1].dataset.id || "0", 10) || 0;
  }

  function append(event) {
    const li = document.createElement("li");
    li.className = "log-" + event.level;
    li.dataset.id = event.id;
    const time = new Date(event.ts).toLocaleTimeString("fr-FR");
    li.innerHTML =
      '<span class="log-time"></span><span class="log-step"></span><span class="log-msg"></span>';
    li.children[0].textContent = time;
    li.children[1].textContent = event.step;
    li.children[2].textContent = event.message;
    log.appendChild(li);
  }

  async function loadSummary() {
    try {
      const resp = await fetch("/jobs/" + jobId + "/summary");
      if (resp.ok) { document.getElementById("summary").innerHTML = await resp.text(); }
    } catch (err) { /* le rechargement de page suffira */ }
  }

  async function tick() {
    let data;
    try {
      const resp = await fetch("/jobs/" + jobId + "/events?after=" + lastId,
        { headers: { Accept: "application/json" } });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      data = await resp.json();
    } catch (err) {
      setTimeout(tick, 5000);
      return;
    }

    if (data.events.length) {
      idleTicks = 0;
      data.events.forEach(function (event) {
        append(event);
        lastId = Math.max(lastId, event.id);
      });
      log.scrollTop = log.scrollHeight;
    } else {
      idleTicks += 1;
    }

    statusPill.textContent = data.status;
    statusPill.className = "pill pill-" +
      ({ done: "ok", error: "ko", running: "warn" }[data.status] || "warn");

    if (!data.running) {
      waiting.classList.add("hidden");
      loadSummary();
      return;
    }
    // Sondage adaptatif : 1,5 s quand ca bouge, jusqu'a 6 s quand c'est calme
    // (l'attente du provisionnement OneDrive dure plusieurs minutes).
    setTimeout(tick, Math.min(1500 + idleTicks * 500, 6000));
  }

  tick();
})();
