const views = ["overview", "modules", "sources", "users", "mcp", "jobs", "audit", "backups", "services"];
const labels = { overview: "Übersicht", modules: "Module", sources: "Quellen", users: "Benutzer", mcp: "MCP", jobs: "Jobs", audit: "Audit", backups: "Backups", services: "Services" };
let current = "overview";
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.json();
}
function esc(value) { const node = document.createElement("span"); node.textContent = String(value ?? ""); return node.innerHTML; }
function badge(ok, yes = "OK", no = "Fehler") { return `<span class="badge ${ok ? "ok" : "bad"}">${ok ? yes : no}</span>`; }
function card(title, body, actions = "") { return `<article class="card"><div class="row between"><h3>${esc(title)}</h3>${actions}</div>${body}</article>`; }
function buttons(items) { return `<div class="toolbar">${items.map(([label, action, danger]) => `<button ${danger ? 'class="danger"' : ""} data-action="${esc(action)}">${esc(label)}</button>`).join("")}</div>`; }

async function renderOverview() {
  const [ready, modules, sources, jobs] = await Promise.all([api("/api/ready"), api("/api/modules"), api("/api/sources"), api("/api/jobs?limit=10")]);
  const healthySources = sources.sources.filter((item) => item.quality?.healthy).length;
  $("content").innerHTML = `<div class="grid">
    ${card("API", badge(ready.ok, "bereit", "nicht bereit"), `<pre>${esc(JSON.stringify(ready, null, 2))}</pre>`)}
    ${card("Module", `<strong>${modules.modules.length}</strong><p class="muted">konfiguriert</p>`)}
    ${card("Quellen", `<strong>${healthySources}/${sources.sources.length}</strong><p class="muted">gesund</p>`)}
    ${card("Jobs", `<strong>${jobs.jobs.filter((item) => item.status === "failed").length}</strong><p class="muted">fehlgeschlagen</p>`)}
  </div>`;
}
async function renderModules() {
  const data = await api("/api/modules/overview");
  $("content").innerHTML = `<div class="row"><button class="primary" data-action="module:new">Modul aus JSON anlegen</button></div><div class="grid">${data.modules.map((item) => card(item.name || item.id,
    `${badge(item.running, "läuft", "gestoppt")} <span class="badge">${esc(item.type)}</span><pre>${esc(JSON.stringify(item, null, 2))}</pre>`,
    buttons([["Start", `module:start:${item.id}`], ["Stop", `module:stop:${item.id}`, true], ["Test", `module:test:${item.id}`], ["Reindex", `module:reindex:${item.id}`], ["JSON bearbeiten", `module:edit:${item.id}`], ["Löschen", `module:delete:${item.id}`, true]])
  ).join("")}</div>`;
}
async function renderSources() {
  const data = await api("/api/sources");
  $("content").innerHTML = `<div class="grid">${data.sources.map((item) => card(item.id,
    `${badge(item.quality?.healthy, "gesund", "nicht produktiv")}<p>${esc(item.target)}</p><pre>${esc(JSON.stringify(item.quality, null, 2))}</pre>`,
    buttons([["Synchronisieren", `source:sync:${item.id}`]])
  ).join("")}</div>`;
}
async function renderUsers() {
  const data = await api("/api/users");
  $("content").innerHTML = `<div class="row"><button class="primary" data-action="user:new">Benutzer anlegen</button></div>
    <div class="grid">${data.users.map((item) => card(item.username,
      `${badge(item.enabled, "aktiv", "deaktiviert")} <span class="badge">${esc(item.role)}</span><p class="muted">Module: ${esc(item.allowed_modules.join(", ") || "-")}<br>Tools: ${esc(item.allowed_tools.join(", ") || "-")}</p>`,
      buttons([["Bearbeiten", `user:edit:${item.username}`]])
    ).join("")}</div>`;
  window.harborUsers = data.users;
}
async function renderMcp() {
  const data = await api("/api/mcp");
  $("content").innerHTML = `<div class="row"><button class="primary" data-action="mcp:install">Paket installieren</button><button data-action="mcp:create">Instanz anlegen</button></div>
    <h3>Pakete</h3><pre class="panel">${esc(JSON.stringify(data.packages, null, 2))}</pre>
    <h3>Instanzen</h3><div class="grid">${data.instances.map((item) => card(item.id,
      `${badge(item.running, "läuft", "gestoppt")}<pre>${esc(JSON.stringify(item, null, 2))}</pre>`,
      buttons([["Start", `mcp:start:${item.id}`], ["Stop", `mcp:stop:${item.id}`, true], ["Restart", `mcp:restart:${item.id}`], ["Rollback", `mcp:rollback:${item.id}`]])
    ).join("")}</div>`;
}
async function renderTable(endpoint, key) {
  const data = await api(endpoint);
  $("content").innerHTML = `<pre class="panel">${esc(JSON.stringify(data[key], null, 2))}</pre>`;
}
async function renderBackups() {
  const data = await api("/api/backups");
  $("content").innerHTML = `<div class="row"><button class="primary" data-action="backup:create">Backup erstellen</button></div><pre class="panel">${esc(JSON.stringify(data.backups, null, 2))}</pre>`;
}
async function renderServices() {
  const data = await api("/api/services");
  $("content").innerHTML = `<div class="grid">${data.services.map((item) => card(item.id,
    `<span class="badge">${esc(item.kind)}</span><p>${esc(item.systemd_mode || "nicht installiert")}</p>`,
    buttons([["Prüfen", `service:check:${item.id}`], ["Start", `service:start:${item.id}`], ["Stop", `service:stop:${item.id}`, true], ["Restart", `service:restart:${item.id}`]])
  ).join("")}</div>`;
}
const renderers = {
  overview: renderOverview, modules: renderModules, sources: renderSources, users: renderUsers, mcp: renderMcp,
  jobs: () => renderTable("/api/jobs", "jobs"), audit: () => renderTable("/api/audit", "events"),
  backups: renderBackups, services: renderServices,
};
async function render() {
  $("title").textContent = labels[current];
  $("notice").textContent = "";
  document.querySelectorAll("#nav button").forEach((item) => item.classList.toggle("active", item.dataset.view === current));
  try { await renderers[current](); } catch (error) { $("notice").textContent = error.message; }
}
async function action(raw) {
  const [kind, verb, ...rest] = raw.split(":");
  const id = rest.join(":");
  let path; let body;
  if (kind === "module" && verb === "new") {
    const rawModule = prompt("Vollständige Modul-Konfiguration als JSON:", '{"id":"docs","type":"docs","path":"data/sources/docs","port":41010}');
    if (!rawModule) return;
    return requestAndRender("/api/modules", "POST", rawModule);
  }
  if (kind === "module" && verb === "edit") {
    try {
      const currentModule = await api(`/api/modules/${encodeURIComponent(id)}`);
      const rawModule = prompt("Modul-Konfiguration bearbeiten:", JSON.stringify(currentModule, null, 2));
      if (!rawModule) return;
      return requestAndRender(`/api/modules/${encodeURIComponent(id)}`, "PUT", rawModule);
    } catch (error) { $("notice").textContent = error.message; return; }
  }
  if (kind === "module" && verb === "delete") {
    if (!confirm(`Modul ${id} wirklich löschen?`)) return;
    return requestAndRender(`/api/modules/${encodeURIComponent(id)}`, "DELETE");
  }
  if (kind === "mcp" && verb === "install") {
    const source = prompt("Serverseitiger Pfad zu mcp-package.json oder Paketverzeichnis:");
    if (!source) return;
    return requestAndRender("/api/mcp/packages/install", "POST", JSON.stringify({ source }));
  }
  if (kind === "mcp" && verb === "create") {
    const instance = prompt("MCP-Instanz als JSON:", '{"id":"instance","package_id":"package","version":"1.0.0","config":{}}');
    if (!instance) return;
    return requestAndRender("/api/mcp/instances", "POST", instance);
  }
  if (kind === "module") path = `/api/modules/${encodeURIComponent(id)}/${verb}`;
  if (kind === "source") path = `/api/sources/${encodeURIComponent(id)}/sync`;
  if (kind === "mcp") path = `/api/mcp/instances/${encodeURIComponent(id)}/${verb}`;
  if (kind === "service") path = `/api/services/${encodeURIComponent(id)}/${verb}`;
  if (kind === "backup") { path = "/api/backups"; body = JSON.stringify({ label: "web" }); }
  if (kind === "user") return openUser(verb === "edit" ? id : "");
  try {
    const result = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body });
    $("notice").textContent = result.message || "Aktion abgeschlossen.";
    await render();
  } catch (error) { $("notice").textContent = error.message; }
}
async function requestAndRender(path, method, body) {
  try {
    await api(path, { method, headers: { "Content-Type": "application/json" }, body });
    $("notice").textContent = "Aktion abgeschlossen.";
    await render();
  } catch (error) { $("notice").textContent = error.message; }
}
function openUser(username) {
  const user = (window.harborUsers || []).find((item) => item.username === username);
  const form = $("user-form");
  form.reset();
  form.username.value = user?.username || "";
  form.username.readOnly = Boolean(user);
  form.role.value = user?.role || "viewer";
  form.enabled.checked = user?.enabled ?? true;
  form.allowed_modules.value = (user?.allowed_modules || ["*"]).join(",");
  form.allowed_tools.value = (user?.allowed_tools || ["*"]).join(",");
  form.dataset.edit = user?.username || "";
  $("user-dialog").showModal();
}
$("user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    username: form.username.value.trim(), password: form.password.value, role: form.role.value, enabled: form.enabled.checked,
    allowed_modules: form.allowed_modules.value.split(",").map((x) => x.trim()).filter(Boolean),
    allowed_tools: form.allowed_tools.value.split(",").map((x) => x.trim()).filter(Boolean),
  };
  const edit = form.dataset.edit;
  try {
    await api(edit ? `/api/users/${encodeURIComponent(edit)}` : "/api/users", {
      method: edit ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    $("user-dialog").close();
    await render();
  } catch (error) { $("notice").textContent = error.message; }
});
$("user-cancel").onclick = () => $("user-dialog").close();
$("content").addEventListener("click", (event) => { const raw = event.target.dataset.action; if (raw) action(raw); });
$("refresh").onclick = render;
for (const view of views) {
  const button = document.createElement("button"); button.textContent = labels[view]; button.dataset.view = view;
  button.onclick = () => { current = view; render(); }; $("nav").append(button);
}
render();
