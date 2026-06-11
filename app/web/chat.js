const state = { sessionId: localStorage.getItem("harbor.session") || "", controller: null, modules: [] };
const $ = (id) => document.getElementById(id);

function message(role, text = "") {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.dataset.raw = text;
  renderMessage(node, text);
  $("messages").append(node);
  $("messages").scrollTop = $("messages").scrollHeight;
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
    copy.textContent = "Kopieren";
    copy.onclick = async () => {
      await navigator.clipboard.writeText(text);
      copy.textContent = "Kopiert";
      setTimeout(() => { copy.textContent = "Kopieren"; }, 1200);
    };
    node.append(copy);
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

async function loadModules() {
  const payload = await api("/api/modules");
  const active = payload.modules.filter((item) => item.enabled);
  state.modules = active;
  $("module-select").replaceChildren(...active.map((item) => new Option(item.name || item.id, item.id)));
  $("module-state").textContent = `${active.length} Module verfügbar`;
}

async function loadSessions() {
  const payload = await api("/api/chat/sessions");
  $("sessions").replaceChildren();
  for (const session of payload.sessions) {
    const row = document.createElement("div");
    row.className = "session-row";
    const button = document.createElement("button");
    button.className = `session ${session.id === state.sessionId ? "active" : ""}`;
    button.innerHTML = `<strong></strong><span class="meta"></span>`;
    button.querySelector("strong").textContent = session.title || "Unbenannter Chat";
    button.querySelector("span").textContent = `${session.message_count} Nachrichten`;
    button.onclick = () => openSession(session.id, session.title);
    const remove = document.createElement("button");
    remove.className = "session-delete danger";
    remove.title = "Chat löschen";
    remove.textContent = "×";
    remove.onclick = async () => {
      if (!confirm(`Chat "${session.title || "Unbenannter Chat"}" löschen?`)) return;
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
  loadSessions();
}

function resetChat() {
  state.sessionId = "";
  localStorage.removeItem("harbor.session");
  $("chat-title").textContent = "Neuer Chat";
  $("messages").replaceChildren();
  loadSessions();
}

function parseEvent(block) {
  const lines = block.split("\n");
  const event = (lines.find((line) => line.startsWith("event:")) || "event: message").slice(6).trim();
  const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
  return { event, data: data ? JSON.parse(data) : {} };
}

async function send(event) {
  event.preventDefault();
  const text = $("prompt").value.trim();
  if (!text || state.controller) return;
  $("prompt").value = "";
  $("notice").textContent = "";
  message("user", text);
  const reply = message("assistant", "");
  state.controller = new AbortController();
  $("stop").classList.remove("hidden");
  const selectedModules = [...$("module-select").selectedOptions].map((item) => item.value);
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: state.sessionId, modules: selectedModules.length ? selectedModules : null }),
      signal: state.controller.signal,
    });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop();
      for (const block of blocks) {
        if (!block.trim()) continue;
        const item = parseEvent(block);
        if (item.event === "meta") {
          state.sessionId = item.data.session_id;
          localStorage.setItem("harbor.session", state.sessionId);
          $("module-state").textContent = item.data.used_modules.length ? `Kontext: ${item.data.used_modules.join(", ")}` : "Ohne Modulkontext";
        } else if (item.event === "token") {
          reply.dataset.raw += item.data.text;
          renderMessage(reply, reply.dataset.raw);
          $("messages").scrollTop = $("messages").scrollHeight;
        } else if (item.event === "error") {
          throw new Error(item.data.detail);
        }
      }
      if (done) break;
    }
    await loadSessions();
  } catch (error) {
    $("notice").textContent = error.name === "AbortError" ? "Antwort abgebrochen." : error.message;
    if (!reply.textContent) reply.remove();
  } finally {
    state.controller = null;
    $("stop").classList.add("hidden");
  }
}

$("composer").addEventListener("submit", send);
$("new-chat").addEventListener("click", resetChat);
$("stop").addEventListener("click", () => state.controller?.abort());
Promise.all([loadModules(), loadSessions()])
  .then(() => state.sessionId && openSession(state.sessionId, "Chat"))
  .catch((error) => { $("notice").textContent = error.message; });
