const views = ["overview", "modules", "connect", "sources", "users", "mcp", "jobs", "audit", "backups", "services", "stellen"];
const labels = { overview: "Overview", modules: "Modules", connect: "Connect", sources: "Sources", users: "Users", mcp: "MCP", jobs: "Jobs", audit: "Audit", backups: "Backups", services: "Betrieb", stellen: "Positions" };
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
function severityBadge(severity) {
  const labels = { ok: "OK", warning: "Warnung", error: "Fehler" };
  const classes = { ok: "ok", warning: "warn", error: "bad" };
  const key = ["ok", "warning", "error"].includes(severity) ? severity : "warning";
  return `<span class="badge ${classes[key]}"><span class="badge-dot"></span>${labels[key]}</span>`;
}
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
function contextValue(primary, secondary = "") {
  if (!primary && !secondary) return `<span class="context-empty">n/a</span>`;
  return `<strong>${esc(primary || secondary)}</strong>${primary && secondary ? `<small>${esc(secondary)}</small>` : ""}`;
}
function renderOpenStackContext(openstack) {
  const scope = openstack?.token_scope || {};
  const ready = Boolean(openstack?.configured);
  const token = Boolean(openstack?.token_configured);
  const scoped = Boolean(scope.project_scoped);
  const validationError = scope.source === "validation_error";
  const severity = ready && token && scoped ? "ok" : validationError || !token ? "error" : "warning";
  const summary = !ready
    ? "Integration nicht eingerichtet"
    : !token
      ? `Token für ${openstack?.token_owner || "diesen Benutzer"} fehlt`
      : validationError
        ? "Token-Validierung fehlgeschlagen"
      : scoped
        ? scope.source === "keystone_validation" ? "Projektkontext aus Keystone" : "Projektkontext aus Token-Metadaten"
        : "Token vorhanden, Projektkontext noch nicht gespeichert";
  const project = scope.project_name || scope.project_id || "";
  const projectDetail = scope.project_name && scope.project_id ? scope.project_id : "";
  const domain = scope.project_domain_name || scope.project_domain_id || scope.user_domain_name || scope.user_domain_id || "";
  const domainDetail = scope.project_domain_name && scope.project_domain_id ? scope.project_domain_id : "";
  const user = scope.user_name || scope.user_id || openstack?.token_owner || "";
  const userDetail = scope.user_name && scope.user_id ? scope.user_id : "";
  return `<section class="context-panel openstack-context">
    <div class="context-head">
      <div>
        <span class="eyebrow">OpenStack Kontext</span>
        <h3>${esc(summary)}</h3>
      </div>
      <div class="row">${severityBadge(severity)}<button data-action="module:openstack">OpenStack</button></div>
    </div>
    ${scope.error ? `<p class="context-error">${esc(scope.error)}</p>` : ""}
    <div class="context-grid">
      <div><span>Domain</span>${contextValue(domain, domainDetail)}</div>
      <div><span>Projekt</span>${contextValue(project, projectDetail)}</div>
      <div><span>Benutzer</span>${contextValue(user, userDetail)}</div>
      <div><span>Ablauf</span>${contextValue(scope.expires_at || "")}</div>
      <div><span>Auth URL</span>${contextValue(openstack?.auth_url || "")}</div>
      <div><span>Region</span>${contextValue(openstack?.region_name || "")}</div>
    </div>
  </section>`;
}

async function renderOverview() {
  const data = await api("/api/dashboard");
  const cache = data.modules.metrics;
  const hitRate = `${Math.round(cache.query_cache_hit_rate * 100)} %`;
  $("content").innerHTML = `
    ${renderOpenStackContext(data.openstack)}
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
          <div><dt>Version</dt><dd>${esc(data.app.version || "n/a")} <span class="muted">(${esc(data.app.git_rev || "unknown")})</span></dd></div>
          <div><dt>CPU Load 1m</dt><dd>${esc(data.stats.cpu_load_1m ?? "n/a")}</dd></div>
          <div><dt>Health Cache</dt><dd>${Math.round(cache.health_cache_hit_rate * 100)} % Treffer</dd></div>
          <div><dt>Ungültige Module</dt><dd>${data.modules.invalid}</dd></div>
        </dl>`)}
      ${card("Recent Activity", data.activity.length ? `<div class="activity-list">${data.activity.slice(0, 8).map((item) => `
        <div><span class="badge">${esc(item.kind)}</span><strong>${esc(item.label)}</strong><time>${esc(item.timestamp)}</time></div>`).join("")}</div>` : empty("Activities appear after operations."))}
    </div>`;
}
async function renderModules() {
  const data = await api("/api/modules/overview");
  $("content").innerHTML = `<div class="page-actions"><div><strong>${data.modules.length} Module</strong><span class="muted">Lokale Worker und externe MCP-Dienste</span></div><div class="row"><button class="primary" data-action="module:new">Modul anlegen</button><button data-action="module:netbox">NetBox einbinden</button><button data-action="module:openstack">OpenStack einbinden</button></div></div>
    <div class="grid">${data.modules.map((item) => {
      const fieldCatalog = item.status?.field_catalog;
      const actions = [["Start", `module:start:${item.id}`], ["Stop", `module:stop:${item.id}`, true], ["Discovery", `module:discover:${item.id}`], ["Test", `module:test:${item.id}`]];
      if (["netbox_mcp", "openstack_mcp"].includes(item.type)) actions.push(["Felder", `module:fields:${item.id}`]);
      actions.push(["Reindex", `module:reindex:${item.id}`], ["Diagnose", `module:diagnose:${item.id}`], ["Edit", `module:edit:${item.id}`], ["Löschen", `module:delete:${item.id}`, true]);
      return card(item.name || item.id,
      `<div class="card-status">${badge(item.running, "läuft", "gestoppt")} <span class="badge">${esc(item.type)}</span>${fieldCatalog ? ` <span class="badge ${fieldCatalog.ok ? "ok" : "bad"}">Felder: ${esc(fieldCatalog.resource_count || 0)}</span>` : ""}</div>
      <p class="endpoint">${esc(item.base_url || item.path || `${item.host}:${item.port}`)}</p>
      ${fieldCatalog ? `<dl class="facts compact"><div><dt>Feldkatalog</dt><dd>${esc(fieldCatalog.updated_at || "noch nicht aktualisiert")}</dd></div></dl>` : ""}
      ${item.validation_errors?.length ? `<p class="error-text">${esc(item.validation_errors.join(" · "))}</p>` : ""}
      <details><summary>Technische Details</summary><pre>${esc(JSON.stringify(item, null, 2))}</pre></details>`,
      buttons(actions)
    );}).join("") || empty("Lege ein Modul an oder binde OpenStack ein.")}</div>`;
}
function connectCounts(modules) {
  return {
    ok: modules.filter((item) => item.severity === "ok").length,
    warning: modules.filter((item) => item.severity === "warning").length,
    error: modules.filter((item) => item.severity === "error").length,
    pending: modules.filter((item) => !item.ran_checks).length,
  };
}
function renderConnectCard(item) {
  const severity = ["ok", "warning", "error"].includes(item.severity) ? item.severity : "warning";
  const checks = Array.isArray(item.checks) ? item.checks : [];
  const steps = Array.isArray(item.next_steps) ? item.next_steps : [];
  const raw = {
    browse: item.browse || null,
    diagnostics: item.diagnostics || null,
    test: item.test || null,
    status: item.status || null,
  };
  const actionItems = [
    [item.ran_checks ? "Erneut testen" : "Connect testen", `connect:run:${item.module_id}`],
    ["Browse JSON", `module:discover:${item.module_id}`],
    ["Diagnose", `module:diagnose:${item.module_id}`],
  ];
  return `<article class="connect-module ${severity}">
    <div class="connect-head">
      <div>
        <h3>${esc(item.name || item.module_id)}</h3>
        <p class="endpoint">${esc(item.endpoint || "-")}</p>
      </div>
      <div class="row">${severityBadge(severity)}<span class="badge">${esc(item.type)}</span><span class="badge">${esc(item.transport)}</span></div>
    </div>
    <p class="connect-summary-text">${esc(item.summary || "Noch keine Diagnose ausgefuehrt.")}</p>
    <div class="connect-checks">${checks.map((check) => {
      const checkSeverity = check.ok ? "ok" : check.severity === "warning" ? "warning" : "error";
      return `<div class="connect-check ${checkSeverity}">
        <strong>${esc(check.label || check.key)}</strong>
        <span>${esc(check.message || "")}</span>
      </div>`;
    }).join("") || `<div class="connect-check warning"><strong>Status</strong><span>Noch keine Checks vorhanden.</span></div>`}</div>
    <div class="connect-steps">
      <strong>Nächste Schritte</strong>
      <ol>${steps.map((step) => `<li>${esc(step)}</li>`).join("") || "<li>Connect-Test starten.</li>"}</ol>
    </div>
    <details><summary>Raw JSON</summary><pre>${esc(JSON.stringify(raw, null, 2))}</pre></details>
    ${buttons(actionItems)}
  </article>`;
}
function renderConnectPage(data) {
  const modules = data.modules || [];
  const counts = connectCounts(modules);
  window.harborConnectDiagnostics = data;
  $("content").innerHTML = `<div class="page-actions">
    <div><strong>${modules.length} Modul-Connects</strong><span class="muted">Admin-only Diagnose fuer Browse, Worker, Test und Logs</span></div>
    <div class="row"><button class="primary" data-action="connect:run-all">Alle testen</button><button data-action="connect:refresh">Basisstatus laden</button></div>
  </div>
  <div class="connect-summary">
    ${metric("OK", counts.ok, "ohne blockierende Fehler", "good")}
    ${metric("Warnungen", counts.warning, "erreichbar, aber unvollstaendig")}
    ${metric("Fehler", counts.error, "blockierende Connect-Probleme", counts.error ? "bad" : "")}
    ${metric("Nicht getestet", counts.pending, "nur Basisstatus geladen")}
  </div>
  <div class="connect-grid">${modules.map(renderConnectCard).join("") || empty("Es sind keine Module konfiguriert.")}</div>`;
}
async function renderConnect() {
  const data = await api("/api/connect-diagnostics/modules");
  renderConnectPage(data);
}
async function runConnectDiagnostic(moduleId, silent = false) {
  if (!silent) $("notice").textContent = `Teste ${moduleId}...`;
  const result = await api(`/api/connect-diagnostics/modules/${encodeURIComponent(moduleId)}`, { method: "POST" });
  const currentData = window.harborConnectDiagnostics || { modules: [] };
  const modules = currentData.modules || [];
  const existing = modules.findIndex((item) => item.module_id === moduleId);
  if (existing >= 0) modules[existing] = result;
  else modules.push(result);
  renderConnectPage({ modules });
  if (!silent) $("notice").textContent = result.summary || "Connect-Test abgeschlossen.";
  return result;
}
async function runAllConnectDiagnostics() {
  const modules = (window.harborConnectDiagnostics?.modules || []).slice();
  if (!modules.length) return renderConnect();
  for (const item of modules) {
    $("notice").textContent = `Teste ${item.module_id}...`;
    try {
      await runConnectDiagnostic(item.module_id, true);
    } catch (error) {
      $("notice").textContent = `${item.module_id}: ${error.message}`;
    }
  }
  $("notice").textContent = "Connect-Tests abgeschlossen.";
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
      buttons([["Edit", `user:edit:${item.username}`]])
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
function renderFieldCatalog(catalog) {
  const resources = Object.values(catalog.resources || {});
  $("fields-status").textContent = catalog.ok ? "OK" : "Fehler im Cache oder Refresh";
  $("fields-status").style.color = catalog.ok ? "var(--accent)" : "var(--danger)";
  $("fields-summary").innerHTML = `<dl class="facts compact">
    <div><dt>Service</dt><dd>${esc(catalog.service || "-")}</dd></div>
    <div><dt>Aktualisiert</dt><dd>${esc(catalog.updated_at || "-")}</dd></div>
    <div><dt>Ressourcen</dt><dd>${esc(catalog.resource_count || 0)}</dd></div>
    <div><dt>Cache</dt><dd>${esc(catalog.cache_path || "-")}</dd></div>
  </dl>${catalog.errors?.length ? `<p class="error-text">${esc(catalog.errors.join(" · "))}</p>` : ""}`;
  $("fields-list").innerHTML = resources.length ? resources.map((resource) => {
    const fields = Array.isArray(resource.fields) ? resource.fields : [];
    const filters = Array.isArray(resource.filters) ? resource.filters : [];
    return `<article class="field-resource">
      <div class="field-resource-head">
        <div><h4>${esc(resource.name)}</h4><span>${esc(resource.endpoint || resource.tool || "-")}</span></div>
        <div class="row">${badge(resource.available !== false, "verfügbar", "nicht verfügbar")}<span class="badge">${esc(resource.field_count || fields.length)} Felder</span></div>
      </div>
      ${resource.error ? `<p class="error-text">${esc(resource.error)}</p>` : ""}
      <div class="field-chip-list">${fields.slice(0, 120).map((field) => `<span title="${esc(field.description || "")}">${esc(field.path)}${field.type ? `<small>${esc(field.type)}</small>` : ""}</span>`).join("") || `<span>Keine Felder beobachtet</span>`}</div>
      ${fields.length > 120 ? `<p class="muted">${fields.length - 120} weitere Felder im Raw-JSON.</p>` : ""}
      ${filters.length ? `<details><summary>Filter</summary><pre>${esc(JSON.stringify(filters, null, 2))}</pre></details>` : ""}
    </article>`;
  }).join("") : empty("Noch kein Feldkatalog vorhanden. Klicke auf Aktualisieren.");
  $("fields-raw").textContent = JSON.stringify(catalog, null, 2);
}
async function openFieldCatalog(moduleId, refresh = false) {
  $("fields-module-id").textContent = moduleId;
  $("fields-status").textContent = refresh ? "Aktualisiere..." : "Lade...";
  $("fields-summary").textContent = "";
  $("fields-list").innerHTML = '<div class="loading compact"><span></span><span></span><span></span></div>';
  $("fields-raw").textContent = "";
  $("fields-refresh").onclick = () => openFieldCatalog(moduleId, true);
  $("fields-refresh").disabled = true;
  if (!$("fields-dialog").open) $("fields-dialog").showModal();
  try {
    const path = refresh
      ? `/api/modules/${encodeURIComponent(moduleId)}/fields/refresh?limit=25`
      : `/api/modules/${encodeURIComponent(moduleId)}/fields`;
    const catalog = await api(path, refresh ? { method: "POST" } : {});
    renderFieldCatalog(catalog);
  } catch (error) {
    $("fields-status").textContent = "Error: " + error.message;
    $("fields-status").style.color = "var(--danger)";
    $("fields-list").innerHTML = "";
  } finally {
    $("fields-refresh").disabled = false;
  }
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
  const services = data.services || [];
  const version = data.version || {};
  const running = services.filter((item) => item.running).length;
  const updateState = version.dirty ? "Lokale Änderungen" : version.update_available ? "Update verfügbar" : version.update_supported ? "Aktuell" : "Kein Upstream";
  $("content").innerHTML = `<div class="page-actions">
    <div><strong>Betrieb</strong><span class="muted">Harbor Runtime, Module und installierte Units</span></div>
    <div class="row"><button class="primary" data-action="system:update">Auf aktuelle Version</button><button class="danger" data-action="system:restart">Harbor neu starten</button></div>
  </div>
  <div class="metric-grid">
    ${metric("Version", version.version || "n/a", `${version.branch || "-"} · ${version.git_rev || "unknown"}`)}
    ${metric("Update", updateState, version.upstream || "kein Git-Upstream", version.update_available ? "bad" : version.dirty ? "bad" : "good")}
    ${metric("Dienste", `${running}/${services.length}`, "laufen / bekannt", running === services.length ? "good" : "")}
    ${metric("Runtime", "Restart", "vollständige Harbor-Runtime")}
  </div>
  <div class="grid">${services.map((item) => {
    const health = item.health || {};
    const systemd = health.systemd || {};
    const runtime = health.runtime || {};
    const actions = item.kind === "harbor"
      ? [["Prüfen", `service:check:${item.id}`], ["Restart", `service:restart:${item.id}`, true]]
      : [["Prüfen", `service:check:${item.id}`], ["Start", `service:start:${item.id}`], ["Stop", `service:stop:${item.id}`, true], ["Restart", `service:restart:${item.id}`]];
    return card(item.display_name || item.id,
      `<div class="card-status">${badge(item.running, "läuft", "gestoppt")} ${badge(item.ok, "gesund", "prüfen")} <span class="badge">${esc(item.kind)}</span></div>
      <dl class="facts compact">
        <div><dt>Profil</dt><dd>${esc(item.id)}</dd></div>
        <div><dt>Unit</dt><dd>${esc(item.systemd_mode === "none" ? "nicht installiert" : item.unit)}</dd></div>
        <div><dt>Modus</dt><dd>${esc(item.systemd_mode || "none")}</dd></div>
        <div><dt>Runtime</dt><dd>${esc(runtime.url || runtime.message || runtime.error || (item.running ? "aktiv" : "inaktiv"))}</dd></div>
        <div><dt>Systemd</dt><dd>${esc(systemd.returncode === undefined ? (systemd.message || "-") : `rc ${systemd.returncode}`)}</dd></div>
      </dl>
      <details><summary>Technische Details</summary><pre>${esc(JSON.stringify(health, null, 2))}</pre></details>`,
      buttons(actions)
    );
  }).join("") || empty("Keine Service-Profile gefunden.")}</div>`;
}
async function renderStellen() {
  const data = await api("/api/stellen");
  $("content").innerHTML = `<div class="page-actions"><div><strong>${data.stellen.length} Stellen</strong><span class="muted">Open positions and job listings</span></div><button class="primary" data-action="stellen:new">Add Position</button></div>
    <div class="grid">${data.stellen.map((item) => card(item.title,
      `<div class="card-status">${badge(item.status === "offen", "Offen", item.status === "besetzt" ? "Besetzt" : "Geschlossen")} <span class="badge">${esc(item.department || "-")}</span></div>
      <p>${esc(item.description || "")}</p>`,
      buttons([["Edit", `stellen:edit:${item.id}`], ["Löschen", `stellen:delete:${item.id}`, true]])
    )).join("") || empty("Lege eine Stelle an.")}</div>`;
}
const renderers = {
  overview: renderOverview, modules: renderModules, connect: renderConnect, sources: renderSources, users: renderUsers, mcp: renderMcp,
  jobs: () => renderTable("/api/jobs", "jobs"), audit: () => renderTable("/api/audit", "events"),
  backups: renderBackups, services: renderServices, stellen: renderStellen,
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
      $("netbox-dialog").showModal();
    } catch (error) { $("notice").textContent = error.message; }
    return;
  }
  if (kind === "module" && verb === "openstack") {
    try {
      const configuration = await api("/api/integrations/openstack");
      const form = $("openstack-form");
      form.reset();
      for (const key of ["auth_url", "region_name", "timeout_seconds", "port"]) {
        form[key].value = configuration[key] ?? "";
      }
      form.token.required = !configuration.token_configured;
      form.token.placeholder = configuration.token_configured ? "Gesetzt; leer lassen zum Beibehalten" : "Token eingeben";
      $("openstack-token-state").textContent = configuration.token_configured
        ? `Ein persönliches Token für ${configuration.token_owner} ist hinterlegt.`
        : `Für ${configuration.token_owner} ist noch kein Token hinterlegt.`;
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
  if (kind === "module" && verb === "fields") {
    await openFieldCatalog(id, false);
    return;
  }
  if (kind === "connect" && verb === "run") {
    try { await runConnectDiagnostic(id); }
    catch (error) { $("notice").textContent = error.message; }
    return;
  }
  if (kind === "connect" && verb === "run-all") {
    try { await runAllConnectDiagnostics(); }
    catch (error) { $("notice").textContent = error.message; }
    return;
  }
  if (kind === "connect" && verb === "refresh") {
    await renderConnect();
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
  if (kind === "system" && verb === "update") {
    await requestAndRender("/api/system/update", "POST");
    return;
  }
  if (kind === "system" && verb === "restart") {
    if (!confirm("Harbor komplett neu starten?")) return;
    await requestAndRender("/api/system/restart", "POST");
    return;
  }
  if (kind === "module") path = `/api/modules/${encodeURIComponent(id)}/${verb}`;
  if (kind === "source") path = `/api/sources/${encodeURIComponent(id)}/sync`;
  if (kind === "mcp") path = `/api/mcp/instances/${encodeURIComponent(id)}/${verb}`;
  if (kind === "service") path = `/api/services/${encodeURIComponent(id)}/${verb}`;
  if (kind === "backup") { path = "/api/backups"; body = JSON.stringify({ label: "web" }); }
  if (kind === "user") return openUser(verb === "edit" ? id : "");
  if (kind === "module" && verb === "diagnose") {
    $("diagnose-module-id").textContent = id;
    $("diagnose-status").textContent = "Lade...";
    $("diagnose-log").textContent = "";
    $("diagnose-health").textContent = "";
    $("diagnose-remote").textContent = "";
    $("diagnose-hint").textContent = "";
    $("diagnose-dialog").showModal();
    api(`/api/modules/${encodeURIComponent(id)}/diagnose`).then((d) => {
      $("diagnose-status").textContent = d.ok ? "OK — keine Probleme erkannt" : "FEHLER — Probleme erkannt";
      $("diagnose-status").style.color = d.ok ? "green" : "red";
      $("diagnose-log").textContent = d.log_tail?.join ? d.log_tail.join("\n") : JSON.stringify(d.log_tail, null, 2);
      $("diagnose-health").textContent = JSON.stringify(d.health, null, 2);
      $("diagnose-remote").textContent = d.remote ? JSON.stringify(d.remote, null, 2) : "N/A";
      $("diagnose-hint").textContent = d.hint || "";
    }).catch((e) => { $("diagnose-status").textContent = "Error: " + e.message; });
    $("diagnose-restart").onclick = () => { $("diagnose-dialog").close(); action(`module:restart:${id}`); };
    return;
  }
  if (kind === "stellen" && verb === "new") { openStellen(); return; }
  if (kind === "stellen" && verb === "edit") { openStellen((await api(`/api/stellen/${encodeURIComponent(id)}`))); return; }
  if (kind === "stellen" && verb === "delete") {
    if (!confirm(`Stelle ${id} wirklich loeschen?`)) return;
    return requestAndRender(`/api/stellen/${encodeURIComponent(id)}`, "DELETE");
  }
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
    token: form.token.value,
    auth_url: form.auth_url.value.trim(), region_name: form.region_name.value.trim(),
    timeout_seconds: Number(form.timeout_seconds.value || 60), port: Number(form.port.value || 0),
  };
  if (await requestAndRender("/api/integrations/openstack", "PUT", JSON.stringify(payload))) $("openstack-dialog").close();
});
$("netbox-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    netbox_url: form.netbox_url.value.trim(),
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
function openStellen(item = null) {
  const form = $("stellen-form");
  form.reset();
  form.title.value = item?.title || "";
  form.department.value = item?.department || "";
  form.description.value = item?.description || "";
  form.status.value = item?.status || "offen";
  form.dataset.edit = item?.id || "";
  $("stellen-dialog").showModal();
}
$("stellen-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    title: form.title.value.trim(),
    department: form.department.value.trim(),
    description: form.description.value.trim(),
    status: form.status.value,
  };
  const edit = form.dataset.edit;
  if (await requestAndRender(edit ? `/api/stellen/${encodeURIComponent(edit)}` : "/api/stellen", edit ? "PUT" : "POST", JSON.stringify(payload))) $("stellen-dialog").close();
});

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
