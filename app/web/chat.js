const state = {
  sessionId: localStorage.getItem("harbor.session") || "",
  controller: null,
  modules: [],
  pendingFrame: 0,
  openstack: null,
};
const $ = (id) => document.getElementById(id);

function scrollMessages() {
  $("messages").scrollTop = $("messages").scrollHeight;
}

function emptyState() {
  if ($("messages").children.length) return;
  const node = document.createElement("div");
  node.className = "empty-state";
  node.innerHTML = "<strong>How can Harbor help?</strong><span>Ask about systems, NetBox data, OpenStack resources, or local documentation.</span>";
  $("messages").append(node);
}

function message(role, text = "") {
  $("messages").querySelector(".empty-state")?.remove();
  const node = document.createElement("article");
  node.className = `message ${role}`;
  node.dataset.raw = text;
  node.setAttribute("aria-label", role === "user" ? "Your message" : "Harbor answer");
  renderMessage(node, text);
  $("messages").append(node);
  scrollMessages();
  return node;
}

function renderMessage(node, text) {
  node.replaceChildren();
  const body = document.createElement("div");
  body.className = "message-body";
  const parts = String(text).split(/```/);
  parts.forEach((part, index) => {
    const element = document.createElement(index % 2 ? "pre" : "div");
    element.textContent = part;
    body.append(element);
  });
  node.append(body);
  if (node.classList.contains("assistant") && text) {
    const copy = document.createElement("button");
    copy.className = "copy-message";
    copy.type = "button";
    copy.textContent = "Copy";
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(text);
        copy.textContent = "Copied";
      } catch {
        copy.textContent = "Not possible";
      }
      setTimeout(() => { copy.textContent = "Copy"; }, 1200);
    };
    node.append(copy);
  }
}

function scheduleReplyRender(node) {
  if (state.pendingFrame) return;
  state.pendingFrame = requestAnimationFrame(() => {
    state.pendingFrame = 0;
    renderMessage(node, node.dataset.raw);
    scrollMessages();
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

async function loadModules() {
  const payload = await api("/api/modules");
  const active = payload.modules.filter((item) => item.enabled);
  const selected = new Set(JSON.parse(localStorage.getItem("harbor.modules") || "[]"));
  state.modules = active;
  $("module-select").replaceChildren(...active.map((item) => {
    const option = new Option(item.name || item.id, item.id);
    option.selected = selected.has(item.id);
    return option;
  }));
  $("module-state").lastChild.textContent = active.length ? `${active.length} modules available` : "No modules available";
  $("module-state").classList.toggle("unavailable", !active.length);
}

async function loadOpenStackCredential() {
  const configuration = await api("/api/integrations/openstack");
  state.openstack = configuration;
  const configured = Boolean(configuration.token_configured);
  const ready = Boolean(configuration.configured);
  const scope = configuration.token_scope || {};
  const domain = scope.project_domain_name || scope.project_domain_id || scope.user_domain_name || scope.user_domain_id || "";
  const project = scope.project_name || scope.project_id || "";
  const user = scope.user_name || scope.user_id || configuration.token_owner || "";
  $("openstack-credential").classList.toggle("ready", configured && ready);
  $("openstack-credential").classList.toggle("warning", !configured || !ready);
  $("openstack-token-status").textContent = !ready
    ? "Integration is not configured yet"
    : configured
      ? `Token active for ${configuration.token_owner}`
      : `Token missing for ${configuration.token_owner}`;
  $("login-context").textContent = ready && configured
    ? `You are logged in to domain ${domain || "n/a"} and project ${project || "n/a"} as user ${user || "n/a"}.`
    : `You are logged in to Harbor as ${configuration.token_owner || "n/a"}; OpenStack project context is missing.`;
  $("openstack-token-open").textContent = configured ? "Renew Token" : "Add Token";
  $("openstack-token-remove").classList.toggle("hidden", !configured);
  $("openstack-token-owner").textContent =
    `This token applies only to Harbor user ${configuration.token_owner}.`;
}

async function loadSessions() {
  const payload = await api("/api/chat/sessions");
  $("sessions").replaceChildren();
  $("session-count").textContent = payload.sessions.length;
  for (const session of payload.sessions) {
    const row = document.createElement("div");
    row.className = "session-row";
    const button = document.createElement("button");
    button.className = `session ${session.id === state.sessionId ? "active" : ""}`;
    button.innerHTML = "<strong></strong><span class=\"meta\"></span>";
    button.querySelector("strong").textContent = session.title || "Untitled Chat";
    button.querySelector("span").textContent = `${session.message_count} messages`;
    button.onclick = () => openSession(session.id, session.title);
    const remove = document.createElement("button");
    remove.className = "session-delete danger";
    remove.title = "Delete chat";
    remove.setAttribute("aria-label", `Delete chat ${session.title || "Untitled Chat"}`);
    remove.textContent = "×";
    remove.onclick = async () => {
      if (!confirm(`Delete chat "${session.title || "Untitled Chat"}"?`)) return;
      await api(`/api/chat/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" });
      if (state.sessionId === session.id) resetChat();
      else loadSessions();
    };
    row.append(button, remove);
    $("sessions").append(row);
  }
}

async function openSession(id, title) {
  const payload = await api(`/api/chat/sessions/${encodeURIComponent(id)}`);
  state.sessionId = id;
  localStorage.setItem("harbor.session", id);
  $("chat-title").textContent = title || "Chat";
  $("messages").replaceChildren();
  payload.messages.forEach((item) => message(item.role, item.content));
  emptyState();
  await loadSessions();
}

function resetChat() {
  state.sessionId = "";
  localStorage.removeItem("harbor.session");
  $("chat-title").textContent = "New Chat";
  $("messages").replaceChildren();
  emptyState();
  loadSessions();
  $("prompt").focus();
}

function parseEvent(block) {
  const lines = block.replaceAll("\r", "").split("\n");
  const event = (lines.find((line) => line.startsWith("event:")) || "event: message").slice(6).trim();
  const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
  return { event, data: data ? JSON.parse(data) : {} };
}

function setBusy(busy) {
  $("send").disabled = busy;
  $("prompt").disabled = busy;
  $("module-select").disabled = busy;
  $("stop").classList.toggle("hidden", !busy);
}

async function send(event) {
  event.preventDefault();
  const text = $("prompt").value.trim();
  if (!text || state.controller) return;
  $("prompt").value = "";
  resizePrompt();
  $("notice").textContent = "";
  message("user", text);
  const reply = message("assistant", "");
  state.controller = new AbortController();
  setBusy(true);
  const selectedModules = [...$("module-select").selectedOptions].map((item) => item.value);
  localStorage.setItem("harbor.modules", JSON.stringify(selectedModules));
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: state.sessionId, modules: selectedModules.length ? selectedModules : null }),
      signal: state.controller.signal,
      cache: "no-store",
    });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`);
    if (!response.body) throw new Error("Streaming is not supported by this browser.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.replaceAll("\r\n", "\n").split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        if (!block.trim()) continue;
        const item = parseEvent(block);
        if (item.event === "meta") {
          state.sessionId = item.data.session_id;
          localStorage.setItem("harbor.session", state.sessionId);
          $("module-state").lastChild.textContent = item.data.used_modules.length ? `Context: ${item.data.used_modules.join(", ")}` : "Without module context";
        } else if (item.event === "token") {
          reply.dataset.raw += item.data.text;
          scheduleReplyRender(reply);
        } else if (item.event === "error") {
          throw new Error(item.data.detail);
        }
      }
      if (done) break;
    }
    if (state.pendingFrame) {
      cancelAnimationFrame(state.pendingFrame);
      state.pendingFrame = 0;
    }
    renderMessage(reply, reply.dataset.raw);
    scrollMessages();
    await loadSessions();
  } catch (error) {
    $("notice").textContent = error.name === "AbortError" ? "Answer stopped." : error.message;
    if (!reply.dataset.raw) reply.remove();
  } finally {
    state.controller = null;
    setBusy(false);
    $("prompt").focus();
  }
}

function resizePrompt() {
  const prompt = $("prompt");
  const visualLines = prompt.value.split("\n").reduce((count, line) => count + Math.max(1, Math.ceil(line.length / 72)), 0);
  prompt.rows = Math.min(8, Math.max(2, visualLines));
}

$("composer").addEventListener("submit", send);
$("new-chat").addEventListener("click", resetChat);
$("stop").addEventListener("click", () => state.controller?.abort());
$("openstack-token-open").addEventListener("click", () => {
  $("openstack-token-form").reset();
  $("openstack-token-dialog").showModal();
  $("openstack-token-form").token.focus();
});
$("openstack-token-cancel").addEventListener("click", () => $("openstack-token-dialog").close());
$("openstack-token-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/integrations/openstack/token", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: form.token.value }),
    });
    $("openstack-token-dialog").close();
    $("notice").textContent = "Your OpenStack user token has been saved.";
    await loadOpenStackCredential();
  } catch (error) {
    $("notice").textContent = error.message;
  }
});
$("openstack-token-remove").addEventListener("click", async () => {
  if (!confirm("Remove your personal OpenStack user token?")) return;
  try {
    await api("/api/integrations/openstack/token", { method: "DELETE" });
    $("openstack-token-dialog").close();
    $("notice").textContent = "Your OpenStack user token has been removed.";
    await loadOpenStackCredential();
  } catch (error) {
    $("notice").textContent = error.message;
  }
});
$("prompt").addEventListener("input", resizePrompt);
$("prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});

emptyState();
Promise.all([loadModules(), loadSessions(), loadOpenStackCredential()])
  .then(() => state.sessionId ? openSession(state.sessionId, "Chat") : $("prompt").focus())
  .catch((error) => { $("notice").textContent = error.message; });
