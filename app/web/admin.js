const views = ["overview", "modules", "sources", "users", "mcp", "jobs", "audit", "backups", "services"];
const labels = { overview: "Übersicht", modules: "Module", sources: "Quellen", users: "Benutzer", mcp: "MCP", jobs: "Jobs", audit: "Audit", backups: "Backups", services: "Services" };
let current = views.includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.status === 204 ? {} : response.json();
}
function esc(value) { const node = document.createElement("span"); node.textContent = String(value ?? ""); return node.innerHTML; }
function badge(ok, yes = "OK", no = "Fehler") { return `<span class="badge ${ok ? "ok" : "bad"}"><span class="badge-dot"></span>${ok ? yes : no}</span>`; }
function card(title, body, actions = "", className = "") { return `<article class="card ${className}"><div class="row between"><h3>${esc(title)}</h3>${actions}</div>${body}</article>`; }
function buttons(items) { return `<div class="toolbar">${items.map(([label, action, danger]) => `<button ${danger ? 'class="danger"' : ""} data-action="${esc(action)}">${esc(label)}</button>`).join("")}</div>`; }
function empty(text) { return `<div class="empty-state"><strong>Noch keine Einträge</strong><span>${esc(text)}</span></div>`; }
function formatUptime(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours} h ${minutes} min` : `${minutes} min`;
}
function metric(label, value, detail, tone = "") {
  return `<article class="metric ${tone}"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(detail)}</small></article>`;
}

async function renderOverview() {
  const data = await api("/api/dashboard");
  const cache = data.modules.metrics;
  const hitRate = `${Math.round(cache.query_cache_hit_rate * 100)} %`;
  $("content").innerHTML = `
    <div class="metric-grid">
      ${metric("LLM", data.llm.connected ? "Verbunden" : "Nicht bereit", data.llm.model || data.llm.detail, data.llm.connected ? "good" : "bad")}
      ${metric("Module", `${data.modules.active}/${data.modules.total}`, "aktiv / konfiguriert", data.modules.invalid ? "bad" : "good")}
      ${metric("Query Cache", hitRate, `${cache.query_cache_hits} Treffer · ${cache.query_cache_misses} Misses`)}
      ${metric("Harbor", formatUptime(data.stats.uptime_seconds), `${data.stats.memory_mb} MiB RAM`)}
    </div>
    <div class="dashboard-grid">
      ${card("Systemzustand", `
        <dl class="facts">
          <div><dt>API</dt><dd>${esc(`${data.app.host}:${data.app.port}`)}</dd></div>
          <div><dt>CPU Load 1m</dt><dd>${esc(data.stats.cpu_load_1m ?? "n/a")}</dd></div>
          <div><dt>Health Cache</dt><dd>${Math.round(cache.health_cache_hit_rate * 100)} % Treffer</dd></div>
          <div><dt>Ungültige Module</dt><dd>${data.modules.invalid}</dd></div>
        </dl>`)}
      ${card("Letzte Aktivität", data.activity.length ? `<div class="activity-list">${data.activity.slice(0, 8).map((item) => `
        <div><span class="badge">${esc(item.kind)}</span><strong>${esc(item.label)}</strong><time>${esc(item.timestamp)}</time></div>`).join("")}</div>` : empty("Aktivitäten erscheinen nach Betriebsaktionen."))}
    </div>`;
}
async function renderModules() {
  const data = await api("/api/modules/overview");
  $("content").innerHTML = `<div class="page-actions"><div><strong>${data.modules.length} Module</strong><span class="muted">Lokale Worker und externe MCP-Dienste</span></div><div class="row"><button class="primary" data-action="module:new">Modul anlegen</button><button data-action="module:netbox">NetBox einbinden</button><button data-action="module:openstack">OpenStack einbinden</button></div></div>
    <div class="grid">${data.modules.map((item) => card(item.name || item.id,
      `<div class="card-status">${badge(item.running, "läuft", "gestoppt")} <span class="badge">${esc(item.type)}</span></div>
      <p class="endpoint">${esc(item.base_url || item.path || `${item.host}:${item.port}`)}</p>
      ${item.validation_errors?.length ? `<p class="error-text">${esc(item.validation_errors.join(" · "))}</p>` : ""}
      <details><summary>Technische Details</summary><pre>${esc(JSON.stringify(item, null, 2))}</pre></details>`,
      buttons([["Start", `module:start:${item.id}`], ["Stop", `module:stop:${item.id}`, true], ["Discovery", `module:discover:${item.id}`], ["Test", `module:test:${item.id}`], ["Reindex", `module:reindex:${item.id}`], ["Bearbeiten", `module:edit:${item.id}`], ["Löschen", `module:delete:${item.id}`, true]])
    )).join("") || empty("Lege ein Modul an oder binde OpenStack ein.")}</div>`;
}
async function renderSources() {
  const data = await api("/api/sources");
  $("content").innerHTML = `<div class="grid">${data.sources.map((item) => card(item.id,
    `${badge(item.quality?.healthy, "gesund", "nicht produktiv")}<p class="endpoint">${esc(item.target)}</p>
    <dl class="facts compact"><div><dt>Dokumente</dt><dd>${esc(item.quality?.document_count ?? "n/a")}</dd></div><div><dt>Status</dt><dd>${esc(item.quality?.reason || "bereit")}</dd></div></dl>`,
    buttons([["Synchronisieren", `source:sync:${item.id}`]])
  )).join("") || empty("Konfigurierte Dokumentquellen erscheinen hier.")}</div>`;
}
async function renderUsers() {
  const data = await api("/api/users");
  window.harborUsers = data.users;
  $("content").innerHTML = `<div class="page-actions"><div><strong>${data.users.length} Benutzer</strong><span class="muted">Rollen und Tool-Berechtigungen</span></div><button class="primary" data-action="user:new">Benutzer anlegen</button></div>
    <div class="grid">${data.users.map((item) => card(item.username,
      `<div class="card-status">${badge(item.enabled, "aktiv", "deaktiviert")} <span class="badge">${esc(item.role)}</span></div>
      <dl class="facts compact"><div><dt>Module</dt><dd>${esc(item.allowed_modules.join(", ") || "-")}</dd></div><div><dt>Tools</dt><dd>${esc(item.allowed_tools.join(", ") || "-")}</dd></div></dl>`,
      buttons([["Bearbeiten", `user:edit:${item.username}`]])
    )).join("")}</div>`;
}
async function renderMcp() {
  const data = await api("/api/mcp");
  window.harborMcpPackages = data.packages;
  $("content").innerHTML = `<div class="page-actions"><div><strong>${data.instances.length} Instanzen</strong><span class="muted">${data.packages.length} installierte Pakete</span></div><div class="row"><button class="primary" data-action="mcp:install">Paket installieren</button><button data-action="mcp:create">Instanz anlegen</button></div></div>
    <h3 class="section-title">Pakete</h3><div class="grid">${data.packages.map((item) => card(`${item.id} ${item.version}`, `<p class="muted">${esc(item.manifest?.driver || "-")} · ${esc((item.manifest?.tools || []).join(", ") || "keine Tools")}</p>`)).join("") || empty("Noch keine Pakete installiert.")}</div>
    <h3 class="section-title">Instanzen</h3><div class="grid">${data.instances.map((item) => card(item.id,
      `<div class="card-status">${badge(item.running, "läuft", "gestoppt")} <span class="badge">${esc(item.package_id)}@${esc(item.package_version)}</span></div>
      <details><summary>Technische Details</summary><pre>${esc(JSON.stringify(item, null, 2))}</pre></details>`,
      buttons([["Start", `mcp:start:${item.id}`], ["Stop", `mcp:stop:${item.id}`, true], ["Restart", `mcp:restart:${item.id}`], ["Rollback", `mcp:rollback:${item.id}`]])
    )).join("") || empty("Lege aus einem installierten Paket eine Instanz an.")}</div>`;
}
function dataTable(rows) {
  if (!rows.length) return empty("Für diese Ansicht liegen keine Daten vor.");
  const preferred = ["id", "timestamp", "created_at", "status", "kind", "action", "target", "actor", "outcome", "message"];
  const available = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const columns = [...preferred.filter((key) => available.includes(key)), ...available.filter((key) => !preferred.includes(key))].slice(0, 7);
  return `<div class="table-wrap"><table><thead><tr>${columns.map((key) => `<th>${esc(key.replaceAll("_", " "))}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((row) => `<tr>${columns.map((key) => `<td>${typeof row[key] === "object" ? `<code>${esc(JSON.stringify(row[key]))}</code>` : esc(row[key] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
async function renderTable(endpoint, key) {
  const data = await api(endpoint);
  $("content").innerHTML = dataTable(data[key]);
}
async function renderBackups() {
  const data = await api("/api/backups");
  $("content").innerHTML = `<div class="page-actions"><div><strong>${data.backups.length} Backups</strong><span class="muted">Online-Sicherungen des Control-State</span></div><button class="primary" data-action="backup:create">Backup erstellen</button></div>${dataTable(data.backups)}`;
}
async function renderServices() {
  const data = await api("/api/services");
  $("content").innerHTML = `<div class="grid">${data.services.map((item) => card(item.id,
    `<span class="badge">${esc(item.kind)}</span><p class="endpoint">${esc(item.systemd_mode || "nicht installiert")}</p>`,
    buttons([["Prüfen", `service:check:${item.id}`], ["Start", `service:start:${item.id}`], ["Stop", `service:stop:${item.id}`, true], ["Restart", `service:restart:${item.id}`]])
  )).join("")}</div>`;
}
const renderers = {
  overview: renderOverview, modules: renderModules, sources: renderSources, users: renderUsers, mcp: renderMcp,
  jobs: () => renderTable("/api/jobs", "jobs"), audit: () => renderTable("/api/audit", "events"),
  backups: renderBackups, services: renderServices,
};
async function render() {
  $("title").textContent = labels[current];
  $("notice").textContent = "";
  $("content").innerHTML = '<div class="loading"><span></span><span></span><span></span></div>';
  $("refresh").disabled = true;
  document.querySelectorAll("#nav button").forEach((item) => item.classList.toggle("active", item.dataset.view === current));
  try { await renderers[current](); } catch (error) { $("content").replaceChildren(); $("notice").textContent = error.message; }
  finally { $("refresh").disabled = false; }
}
async function action(raw) {
  const [kind, verb, ...rest] = raw.split(":");
  const id = rest.join(":");
  let path; let body;
  if (kind === "module" && verb === "new") return openModule();
  if (kind === "module" && verb === "netbox") {
    try {
      const configuration = await api("/api/integrations/netbox");
      const form = $("netbox-form");
      form.reset();
      form.netbox_url.value = configuration.netbox_url || "";
      form.timeout_seconds.value = configuration.timeout_seconds || 30;
      form.port.value = configuration.port || 0;
      form.token.placeholder = configuration.token_configured ? "Gesetzt; leer lassen zum Beibehalten" : "Read-only Token";
      $("netbox-token-state").textContent = configuration.token_configured ? "Ein Token ist sicher hinterlegt." : "Kein Token hinterlegt; nur für öffentliche NetBox-APIs geeignet.";
      $("netbox-dialog").showModal();
    } catch (error) { $("notice").textContent = error.message; }
    return;
  }
  if (kind === "module" && verb === "openstack") {
    try {
      const configuration = await api("/api/integrations/openstack");
      const form = $("openstack-form");
      form.reset();
      for (const key of ["project_id", "project_name", "project_domain_name", "auth_url", "region_name", "timeout_seconds", "port"]) {
        form[key].value = configuration[key] ?? "";
      }
      form.token.required = !configuration.token_configured;
      form.token.placeholder = configuration.token_configured ? "Gesetzt; leer lassen zum Beibehalten" : "Token eingeben";
      $("openstack-token-state").textContent = configuration.token_configured ? "Ein Token ist sicher hinterlegt." : "Noch kein Token hinterlegt.";
      $("openstack-dialog").showModal();
    } catch (error) { $("notice").textContent = error.message; }
    return;
  }
  if (kind === "module" && verb === "edit") {
    try { return openModule(await api(`/api/modules/${encodeURIComponent(id)}`)); }
    catch (error) { $("notice").textContent = error.message; return; }
  }
  if (kind === "module" && verb === "delete") {
    if (!confirm(`Modul ${id} wirklich löschen?`)) return;
    return requestAndRender(`/api/modules/${encodeURIComponent(id)}`, "DELETE");
  }
  if (kind === "module" && verb === "discover") {
    try {
      const result = await api(`/api/modules/${encodeURIComponent(id)}/discover`, { method: "POST" });
      $("discovery-title").textContent = `Discovery: ${id}`;
      $("discovery-result").textContent = JSON.stringify(result, null, 2);
      $("discovery-dialog").showModal();
    } catch (error) { $("notice").textContent = error.message; }
    return;
  }
  if (kind === "mcp" && verb === "install") {
    $("mcp-package-form").reset(); $("mcp-package-dialog").showModal(); return;
  }
  if (kind === "mcp" && verb === "create") {
    const packages = window.harborMcpPackages || [];
    const form = $("mcp-instance-form");
    const select = form.package_id;
    form.reset();
    select.replaceChildren(...packages.map((item) => new Option(`${item.id} @ ${item.version}`, `${item.id}|${item.version}`)));
    if (packages[0]) { select.value = `${packages[0].id}|${packages[0].version}`; form.version.value = packages[0].version; }
    $("mcp-instance-dialog").showModal(); return;
  }
  if (kind === "module") path = `/api/modules/${encodeURIComponent(id)}/${verb}`;
  if (kind === "source") path = `/api/sources/${encodeURIComponent(id)}/sync`;
  if (kind === "mcp") path = `/api/mcp/instances/${encodeURIComponent(id)}/${verb}`;
  if (kind === "service") path = `/api/services/${encodeURIComponent(id)}/${verb}`;
  if (kind === "backup") { path = "/api/backups"; body = JSON.stringify({ label: "web" }); }
  if (kind === "user") return openUser(verb === "edit" ? id : "");
  await requestAndRender(path, "POST", body);
}
async function requestAndRender(path, method, body) {
  try {
    const result = await api(path, { method, headers: { "Content-Type": "application/json" }, body });
    $("notice").textContent = result.message || "Aktion abgeschlossen.";
    await render();
    return true;
  } catch (error) { $("notice").textContent = error.message; return false; }
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
function openModule(module = null) {
  const form = $("module-form");
  form.reset();
  form.id.value = module?.id || "";
  form.id.readOnly = Boolean(module);
  form.name.value = module?.name || "";
  form.type.value = module?.type || "docs";
  form.path.value = module?.path || "";
  form.base_url.value = module?.base_url || "";
  form.port.value = module?.port || 0;
  form.top_k.value = module?.top_k || 5;
  form.timeout_seconds.value = module?.timeout_seconds || 30;
  form.notes.value = module?.notes || "";
  form.dataset.edit = module?.id || "";
  $("module-dialog").showModal();
}
$("module-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const type = form.type.value;
  const payload = {
    id: form.id.value.trim(), name: form.name.value.trim(), type,
    transport: type === "mcp_http" ? "remote" : "local", remote_protocol: type === "mcp_http" ? "mcp" : "auto",
    path: form.path.value.trim(), base_url: form.base_url.value.trim(), port: Number(form.port.value || 0),
    top_k: Number(form.top_k.value || 5), timeout_seconds: Number(form.timeout_seconds.value || 30), notes: form.notes.value.trim(),
  };
  const edit = form.dataset.edit;
  if (await requestAndRender(edit ? `/api/modules/${encodeURIComponent(edit)}` : "/api/modules", edit ? "PUT" : "POST", JSON.stringify(payload))) $("module-dialog").close();
});
$("openstack-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    project_id: form.project_id.value.trim(), project_name: form.project_name.value.trim(),
    project_domain_name: form.project_domain_name.value.trim(), token: form.token.value,
    auth_url: form.auth_url.value.trim(), region_name: form.region_name.value.trim(),
    timeout_seconds: Number(form.timeout_seconds.value || 60), port: Number(form.port.value || 0),
  };
  if (await requestAndRender("/api/integrations/openstack", "PUT", JSON.stringify(payload))) $("openstack-dialog").close();
});
$("netbox-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    netbox_url: form.netbox_url.value.trim(), token: form.token.value,
    timeout_seconds: Number(form.timeout_seconds.value || 30), port: Number(form.port.value || 0),
  };
  if (await requestAndRender("/api/integrations/netbox", "PUT", JSON.stringify(payload))) $("netbox-dialog").close();
});
$("mcp-package-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (await requestAndRender("/api/mcp/packages/install", "POST", JSON.stringify({ source: event.currentTarget.source.value.trim() }))) $("mcp-package-dialog").close();
});
$("mcp-instance-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const [packageId, selectedVersion] = form.package_id.value.split("|");
  const env = {};
  for (const line of form.environment.value.split("\n")) {
    const [key, ...value] = line.split("=");
    if (key.trim()) env[key.trim()] = value.join("=").trim();
  }
  const payload = { id: form.id.value.trim(), package_id: packageId, version: form.version.value.trim() || selectedVersion, config: { env } };
  if (await requestAndRender("/api/mcp/instances", "POST", JSON.stringify(payload))) $("mcp-instance-dialog").close();
});
$("user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    username: form.username.value.trim(), password: form.password.value, role: form.role.value, enabled: form.enabled.checked,
    allowed_modules: form.allowed_modules.value.split(",").map((x) => x.trim()).filter(Boolean),
    allowed_tools: form.allowed_tools.value.split(",").map((x) => x.trim()).filter(Boolean),
  };
  const edit = form.dataset.edit;
  if (await requestAndRender(edit ? `/api/users/${encodeURIComponent(edit)}` : "/api/users", edit ? "PUT" : "POST", JSON.stringify(payload))) $("user-dialog").close();
});
$("user-cancel").onclick = () => $("user-dialog").close();
$("mcp-instance-form").package_id.addEventListener("change", (event) => { $("mcp-instance-form").version.value = event.target.value.split("|")[1] || ""; });
document.querySelectorAll("[data-close]").forEach((button) => { button.onclick = () => $(button.dataset.close).close(); });
$("content").addEventListener("click", (event) => { const button = event.target.closest("[data-action]"); if (button) action(button.dataset.action); });
$("refresh").onclick = render;
for (const view of views) {
  const button = document.createElement("button");
  button.textContent = labels[view]; button.dataset.view = view;
  button.onclick = () => { current = view; location.hash = view; render(); };
  $("nav").append(button);
}
render();
