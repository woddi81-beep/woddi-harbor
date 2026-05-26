from __future__ import annotations

import html
import json
import os
import time
from collections import deque
from pathlib import Path
from textwrap import dedent
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import require_role
from .config import HarborSettings, LOG_DIR, ModuleConfig, ModuleSource, find_module, load_modules, load_settings, system_prompt
from .llm import complete_chat, extract_chat_content
from .modules import (
    discover_remote_module,
    execute_module,
    list_module_overview,
    module_diagnostics,
    module_log_path,
    module_status,
    module_test,
    remove_module,
    restart_module,
    start_module,
    stop_module,
    upsert_module,
)


APP_STARTED_AT = time.time()
RECENT_ACTIVITY: deque[dict[str, Any]] = deque(maxlen=25)
DEFAULT_LOG_PATH = Path("~/.harbor/logs/harbor.log").expanduser()


class ExecuteRequest(BaseModel):
    action: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    modules: list[str] | None = None


class ModuleSourceRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=4000)
    label: str = ""
    enabled: bool = True


class ModuleUpsertRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=32)
    enabled: bool = True
    name: str = ""
    provider: str = ""
    transport: str = "local"
    remote_protocol: str = "auto"
    path: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    host: str = "127.0.0.1"
    port: int = 0
    timeout_seconds: float = 30.0
    top_k: int = 5
    notes: str = ""
    tool_names: list[str] = Field(default_factory=list)
    test_action: str = ""
    test_payload: dict[str, Any] = Field(default_factory=dict)
    test_expect_contains: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    sources: list[ModuleSourceRequest] = Field(default_factory=list)


def _record_activity(kind: str, label: str, detail: str = "") -> None:
    RECENT_ACTIVITY.appendleft(
        {
            "kind": kind,
            "label": label,
            "detail": detail,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
    )


def _llm_health(settings: HarborSettings) -> dict[str, Any]:
    if not settings.llm.base_url or not settings.llm.model:
        return {"connected": False, "status": "unconfigured", "detail": "LLM ist nicht konfiguriert."}
    headers = {"Content-Type": "application/json"}
    if settings.llm.api_key:
        headers["Authorization"] = f"Bearer {settings.llm.api_key}"
    elif settings.llm.api_key_env:
        secret = os.getenv(settings.llm.api_key_env, "").strip()
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
    base_url = settings.llm.base_url.rstrip("/")
    try:
        with httpx.Client(timeout=min(settings.llm.timeout_seconds, 4.0)) as client:
            if "/api" in base_url or "11434" in base_url:
                response = client.get(f"{base_url}/tags", headers=headers)
            else:
                response = client.get(f"{base_url}/models", headers=headers)
            response.raise_for_status()
        return {
            "connected": True,
            "status": "connected",
            "detail": f"{settings.llm.model} via {base_url}",
        }
    except Exception as exc:
        return {
            "connected": False,
            "status": "error",
            "detail": str(exc),
        }


def _system_stats() -> dict[str, Any]:
    uptime_seconds = max(0, int(time.time() - APP_STARTED_AT))
    memory_mb = 0.0
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    memory_kb = int(line.split()[1])
                    memory_mb = round(memory_kb / 1024, 1)
                    break
    except Exception:
        memory_mb = 0.0
    cpu_load = None
    try:
        cpu_load = round(os.getloadavg()[0], 2)
    except Exception:
        cpu_load = None
    return {
        "cpu_load_1m": cpu_load,
        "memory_mb": memory_mb,
        "uptime_seconds": uptime_seconds,
    }


def _dashboard_payload() -> dict[str, Any]:
    settings = load_settings()
    modules = list_module_overview()
    llm = _llm_health(settings)
    active_modules = [module for module in modules if module["running"]]
    invalid_modules = [module for module in modules if module["validation_errors"]]
    return {
        "app": {
            "name": settings.name,
            "host": settings.host,
            "port": settings.port,
        },
        "llm": llm,
        "modules": {
            "total": len(modules),
            "active": len(active_modules),
            "enabled": len([module for module in modules if module["enabled"]]),
            "invalid": len(invalid_modules),
            "items": modules,
        },
        "activity": list(RECENT_ACTIVITY),
        "stats": _system_stats(),
    }


def _read_harbor_log() -> dict[str, Any]:
    candidates = [DEFAULT_LOG_PATH, LOG_DIR / "harbor.log"]
    for path in candidates:
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return {"path": str(path), "content": "\n".join(lines[-200:])}
    return {"path": str(DEFAULT_LOG_PATH), "content": "Logdatei nicht gefunden."}


def _chat_page_html(settings: HarborSettings) -> str:
    title = html.escape(settings.name)
    return dedent(
        f"""\
        <!DOCTYPE html>
        <html lang="de">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>{title} Chat</title>
          <style>
            :root {{
              color-scheme: light;
              --bg: #f4efe6;
              --panel: #fffdf8;
              --line: #d8cfbf;
              --text: #1f2328;
              --muted: #66604f;
              --accent: #17624a;
              --accent-2: #e4f1eb;
              --user: #f0ede4;
            }}
            * {{ box-sizing: border-box; }}
            body {{
              margin: 0;
              font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
              background:
                radial-gradient(circle at top left, #fff7e8 0, transparent 32%),
                linear-gradient(180deg, #f8f2e8 0%, var(--bg) 100%);
              color: var(--text);
            }}
            .shell {{
              max-width: 960px;
              margin: 0 auto;
              min-height: 100vh;
              padding: 24px 16px 32px;
            }}
            .topbar {{
              display: flex;
              justify-content: space-between;
              gap: 16px;
              align-items: center;
              margin-bottom: 18px;
            }}
            .brand h1 {{ margin: 0; font-size: 1.5rem; }}
            .brand p {{ margin: 4px 0 0; color: var(--muted); }}
            .topbar a {{
              color: var(--accent);
              text-decoration: none;
              font-weight: 600;
            }}
            .panel {{
              background: rgba(255, 253, 248, 0.92);
              border: 1px solid var(--line);
              border-radius: 18px;
              box-shadow: 0 16px 36px rgba(58, 45, 23, 0.08);
            }}
            .chat-log {{
              min-height: 52vh;
              padding: 18px;
              display: flex;
              flex-direction: column;
              gap: 12px;
            }}
            .message {{
              border-radius: 14px;
              padding: 12px 14px;
              line-height: 1.5;
              white-space: pre-wrap;
            }}
            .message.user {{
              background: var(--user);
              align-self: flex-end;
              max-width: 82%;
            }}
            .message.assistant {{
              background: var(--accent-2);
              align-self: flex-start;
              max-width: 88%;
            }}
            .message.system {{
              background: transparent;
              border: 1px dashed var(--line);
              color: var(--muted);
            }}
            form {{
              margin-top: 16px;
              display: grid;
              gap: 12px;
            }}
            textarea {{
              width: 100%;
              min-height: 120px;
              resize: vertical;
              border: 1px solid var(--line);
              border-radius: 16px;
              padding: 14px;
              font: inherit;
              background: var(--panel);
              color: var(--text);
            }}
            .controls {{
              display: flex;
              gap: 12px;
              align-items: center;
              justify-content: space-between;
              flex-wrap: wrap;
            }}
            button {{
              border: 0;
              border-radius: 999px;
              background: var(--accent);
              color: white;
              padding: 10px 18px;
              font: inherit;
              font-weight: 600;
              cursor: pointer;
            }}
            button[disabled] {{
              opacity: 0.6;
              cursor: wait;
            }}
            #status {{ color: var(--muted); min-height: 1.25rem; }}
            @media (max-width: 640px) {{
              .chat-log {{ min-height: 48vh; }}
              .message.user, .message.assistant {{ max-width: 100%; }}
            }}
          </style>
        </head>
        <body>
          <main class="shell">
            <header class="topbar">
              <div class="brand">
                <h1>{title}</h1>
                <p>Chat-Ansicht fuer Benutzer</p>
              </div>
              <a href="/admin">Admin</a>
            </header>
            <section class="panel">
              <div id="chatLog" class="chat-log">
                <div class="message system">Harbor ist bereit. Der Verlauf bleibt in diesem Browser-Tab.</div>
              </div>
            </section>
            <form id="chatForm">
              <textarea id="message" name="message" placeholder="Frage eingeben..." required></textarea>
              <div class="controls">
                <div id="status"></div>
                <button id="sendButton" type="submit">Senden</button>
              </div>
            </form>
          </main>
          <script>
            const chatForm = document.getElementById("chatForm");
            const messageInput = document.getElementById("message");
            const chatLog = document.getElementById("chatLog");
            const sendButton = document.getElementById("sendButton");
            const statusNode = document.getElementById("status");

            function appendMessage(role, text) {{
              const node = document.createElement("div");
              node.className = `message ${{role}}`;
              node.textContent = text;
              chatLog.appendChild(node);
              chatLog.scrollTop = chatLog.scrollHeight;
            }}

            chatForm.addEventListener("submit", async (event) => {{
              event.preventDefault();
              const message = messageInput.value.trim();
              if (!message) return;
              appendMessage("user", message);
              messageInput.value = "";
              statusNode.textContent = "Antwort wird geladen...";
              sendButton.disabled = true;
              try {{
                const response = await fetch("/api/chat", {{
                  method: "POST",
                  headers: {{ "Content-Type": "application/json" }},
                  body: JSON.stringify({{ message }})
                }});
                const payload = await response.json().catch(() => ({{ detail: "Ungueltige Server-Antwort." }}));
                if (!response.ok) {{
                  throw new Error(payload.detail || "Chat-Anfrage fehlgeschlagen.");
                }}
                const used = payload.used_modules?.length ? `\\n\\nQuellen: ${{payload.used_modules.join(", ")}}` : "";
                appendMessage("assistant", `${{payload.reply || "(leer)"}}${{used}}`);
                statusNode.textContent = "";
              }} catch (error) {{
                appendMessage("assistant", `Fehler: ${{error.message}}`);
                statusNode.textContent = "Anfrage fehlgeschlagen.";
              }} finally {{
                sendButton.disabled = false;
                messageInput.focus();
              }}
            }});
          </script>
        </body>
        </html>
        """
    )


def _admin_page_html(settings: HarborSettings) -> str:
    title = html.escape(settings.name)
    return dedent(
        f"""\
        <!DOCTYPE html>
        <html lang="de">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>{title} Admin</title>
          <style>
            :root {{
              color-scheme: light;
              --bg: #eef1eb;
              --panel: #fffefb;
              --line: #cad2c3;
              --text: #18221a;
              --muted: #5c685a;
              --success: #295f3c;
              --warning: #8d6212;
              --danger: #9b2f35;
              --info: #2d6178;
              --disabled: #6c746c;
              --accent: #14332a;
            }}
            * {{ box-sizing: border-box; }}
            body {{
              margin: 0;
              font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
              color: var(--text);
              background:
                radial-gradient(circle at top left, rgba(255, 247, 226, 0.9), transparent 32%),
                linear-gradient(180deg, rgba(20, 51, 42, 0.08), transparent 24%),
                linear-gradient(180deg, #f8fafc 0%, var(--bg) 100%);
            }}
            .shell {{
              max-width: 1380px;
              margin: 0 auto;
              padding: 24px 16px 40px;
            }}
            .topbar {{
              display: flex;
              justify-content: space-between;
              gap: 16px;
              align-items: center;
              margin-bottom: 18px;
            }}
            .topbar h1 {{ margin: 0; font-size: 1.6rem; }}
            .topbar p {{ margin: 4px 0 0; color: var(--muted); }}
            .topbar a {{
              color: var(--info);
              text-decoration: none;
              font-weight: 600;
            }}
            .overview {{
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
              gap: 12px;
              margin-bottom: 16px;
            }}
            .summary-bar {{
              display: flex;
              gap: 10px;
              flex-wrap: wrap;
              margin-bottom: 16px;
            }}
            .summary-pill {{
              border-radius: 999px;
              border: 1px solid var(--line);
              padding: 8px 12px;
              background: rgba(255, 254, 251, 0.92);
              font-size: 0.92rem;
            }}
            .summary-pill.ok {{
              border-color: rgba(41, 95, 60, 0.25);
              color: var(--success);
            }}
            .summary-pill.warn {{
              border-color: rgba(141, 98, 18, 0.25);
              color: var(--warning);
            }}
            .summary-pill.fail {{
              border-color: rgba(155, 47, 53, 0.25);
              color: var(--danger);
            }}
            .summary-pill.info {{
              border-color: rgba(45, 97, 120, 0.25);
              color: var(--info);
            }}
            .metric {{
              background: rgba(255, 254, 251, 0.92);
              border: 1px solid var(--line);
              border-radius: 18px;
              padding: 16px;
              box-shadow: 0 10px 24px rgba(24, 34, 26, 0.06);
            }}
            .metric strong {{
              display: block;
              font-size: 1.4rem;
              margin-top: 4px;
            }}
            .toolbar {{
              display: flex;
              justify-content: space-between;
              gap: 12px;
              align-items: center;
              margin-bottom: 14px;
              flex-wrap: wrap;
            }}
            .toolbar button {{
              border: 1px solid var(--line);
              background: var(--panel);
              border-radius: 999px;
              padding: 10px 16px;
              font: inherit;
              cursor: pointer;
            }}
            #status {{ color: var(--muted); }}
            .grid {{
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
              gap: 16px;
            }}
            .layout {{
              display: grid;
              grid-template-columns: minmax(320px, 380px) 1fr;
              gap: 18px;
              align-items: start;
            }}
            .stack {{
              display: grid;
              gap: 16px;
            }}
            .card {{
              background: rgba(255, 255, 255, 0.94);
              border: 1px solid var(--line);
              border-radius: 18px;
              padding: 18px;
              box-shadow: 0 14px 32px rgba(28, 45, 64, 0.08);
            }}
            .card-head {{
              display: flex;
              justify-content: space-between;
              align-items: flex-start;
              gap: 12px;
            }}
            .card h2 {{
              margin: 0 0 4px;
              font-size: 1.1rem;
            }}
            .meta {{
              color: var(--muted);
              font-size: 0.95rem;
            }}
            .badge {{
              border-radius: 999px;
              padding: 5px 10px;
              font-size: 0.82rem;
              font-weight: 700;
              white-space: nowrap;
              border: 1px solid currentColor;
            }}
            .badge.success {{ color: var(--success); background: rgba(29, 107, 68, 0.08); }}
            .badge.warning {{ color: var(--warning); background: rgba(148, 98, 0, 0.1); }}
            .badge.danger {{ color: var(--danger); background: rgba(161, 47, 47, 0.08); }}
            .badge.info {{ color: var(--info); background: rgba(35, 95, 154, 0.08); }}
            .badge.muted {{ color: var(--disabled); background: rgba(107, 114, 128, 0.08); }}
            .signals {{
              display: flex;
              gap: 8px;
              flex-wrap: wrap;
              margin: 12px 0 4px;
            }}
            .signal {{
              display: inline-flex;
              align-items: center;
              gap: 7px;
              border-radius: 999px;
              border: 1px solid var(--line);
              padding: 5px 10px;
              font-size: 0.82rem;
              background: rgba(255,255,255,0.75);
            }}
            .signal-dot {{
              width: 10px;
              height: 10px;
              border-radius: 999px;
              display: inline-block;
              background: var(--disabled);
            }}
            .signal.ok .signal-dot {{ background: var(--success); }}
            .signal.warn .signal-dot {{ background: var(--warning); }}
            .signal.fail .signal-dot {{ background: var(--danger); }}
            dl {{
              display: grid;
              grid-template-columns: auto 1fr;
              gap: 8px 10px;
              margin: 16px 0;
            }}
            dt {{ color: var(--muted); }}
            dd {{ margin: 0; word-break: break-word; }}
            .errors {{
              margin: 0 0 14px;
              padding-left: 18px;
              color: var(--danger);
            }}
            .form-grid {{
              display: grid;
              gap: 10px;
            }}
            .field {{
              display: grid;
              gap: 6px;
            }}
            .field label {{
              color: var(--muted);
              font-size: 0.92rem;
            }}
            input, select, textarea {{
              width: 100%;
              border: 1px solid var(--line);
              border-radius: 12px;
              padding: 10px 12px;
              font: inherit;
              background: var(--panel);
              color: var(--text);
            }}
            textarea {{
              min-height: 96px;
              resize: vertical;
            }}
            .actions {{
              display: flex;
              gap: 8px;
              flex-wrap: wrap;
            }}
            .actions button {{
              border: 0;
              border-radius: 10px;
              color: white;
              padding: 9px 12px;
              font: inherit;
              cursor: pointer;
            }}
            .actions button[data-action="start"] {{ background: var(--success); }}
            .actions button[data-action="stop"] {{ background: var(--danger); }}
            .actions button[data-action="restart"] {{ background: var(--info); }}
            .actions button[data-action="discover"] {{ background: var(--accent); }}
            .actions button[data-action="diagnose"] {{ background: #495057; }}
            .actions button[data-action="delete"] {{ background: #5a1d24; }}
            .secondary {{
              background: transparent;
              border: 1px solid var(--line);
              color: var(--text);
            }}
            .detail {{
              margin-top: 14px;
              padding: 12px;
              border-radius: 12px;
              background: #f7f8f4;
              border: 1px solid var(--line);
              white-space: pre-wrap;
              font-family: "IBM Plex Mono", monospace;
              font-size: 0.86rem;
              overflow-x: auto;
            }}
            .hint {{
              color: var(--muted);
              font-size: 0.92rem;
              margin-top: 8px;
            }}
            .card section + section {{
              margin-top: 16px;
              padding-top: 16px;
              border-top: 1px solid rgba(202, 210, 195, 0.8);
            }}
            @media (max-width: 980px) {{
              .layout {{
                grid-template-columns: 1fr;
              }}
            }}
          </style>
        </head>
        <body>
          <main class="shell">
            <header class="topbar">
              <div>
                <h1>{title}</h1>
                <p>Admin-Ansicht fuer Module, MCPs, Addons und Prozesssteuerung</p>
              </div>
              <a href="/chat">Chat</a>
            </header>
            <section class="overview" id="overview"></section>
            <section class="summary-bar" id="summaryBar"></section>
            <div class="toolbar">
              <div class="actions">
                <button id="refreshButton" type="button" class="secondary">Aktualisieren</button>
                <button id="newNetboxButton" type="button" class="secondary">NetBox MCP Vorlage</button>
              </div>
              <div id="status"></div>
            </div>
            <section class="layout">
              <aside class="stack">
                <article class="card">
                  <h2>Neues Modul / Addon</h2>
                  <form id="createForm" class="form-grid">
                    <div class="field"><label for="create-id">ID</label><input id="create-id" name="id" required></div>
                    <div class="field"><label for="create-name">Name</label><input id="create-name" name="name"></div>
                    <div class="field"><label for="create-type">Typ</label>
                      <select id="create-type" name="type">
                        <option value="docs">docs</option>
                        <option value="maildir">maildir</option>
                        <option value="mcp_http">mcp_http</option>
                      </select>
                    </div>
                    <div class="field"><label for="create-provider">Provider</label><input id="create-provider" name="provider" placeholder="generic oder netbox-mcp-server"></div>
                    <div class="field"><label for="create-transport">Transport</label>
                      <select id="create-transport" name="transport">
                        <option value="local">local</option>
                        <option value="remote">remote</option>
                      </select>
                    </div>
                    <div class="field"><label for="create-remote-protocol">Remote Protocol</label>
                      <select id="create-remote-protocol" name="remote_protocol">
                        <option value="auto">auto</option>
                        <option value="harbor_execute">harbor_execute</option>
                        <option value="mcp">mcp</option>
                      </select>
                    </div>
                    <div class="field"><label for="create-path">Lokaler Pfad</label><input id="create-path" name="path"></div>
                    <div class="field"><label for="create-base-url">Remote URL</label><input id="create-base-url" name="base_url" placeholder="http://127.0.0.1:8000/mcp"></div>
                    <div class="field"><label for="create-host">Host</label><input id="create-host" name="host" value="127.0.0.1"></div>
                    <div class="field"><label for="create-port">Port</label><input id="create-port" name="port" type="number" value="0"></div>
                    <div class="field"><label for="create-timeout">Timeout Sekunden</label><input id="create-timeout" name="timeout_seconds" type="number" step="0.1" value="30"></div>
                    <div class="field"><label for="create-top-k">Top K</label><input id="create-top-k" name="top_k" type="number" value="5"></div>
                    <div class="field"><label for="create-api-key-env">API Key Env</label><input id="create-api-key-env" name="api_key_env"></div>
                    <div class="field"><label for="create-tool-names">Tool-Namen</label><input id="create-tool-names" name="tool_names" placeholder="get_objects,get_object_by_id"></div>
                    <div class="field"><label for="create-test-action">Test Action</label><input id="create-test-action" name="test_action" placeholder="discover oder search"></div>
                    <div class="field"><label for="create-test-payload">Test Payload JSON</label><textarea id="create-test-payload" name="test_payload">{{}}</textarea></div>
                    <div class="field"><label for="create-test-expect">Erwartete Begriffe</label><input id="create-test-expect" name="test_expect_contains" placeholder="router,site"></div>
                    <div class="field"><label for="create-settings">Settings JSON</label><textarea id="create-settings" name="settings">{{}}</textarea></div>
                    <div class="field"><label for="create-notes">Notizen</label><textarea id="create-notes" name="notes"></textarea></div>
                    <div class="field"><label><input id="create-enabled" name="enabled" type="checkbox" checked> aktiviert</label></div>
                    <button type="submit">Anlegen</button>
                  </form>
                  <p class="hint">Fuer lokale Suchmodule reicht meist `type`, `id`, `path` und `port`. Fuer standardkonforme MCP-Server wie NetBox setze `type=mcp_http`, `transport=remote`, `remote_protocol=mcp` und die komplette `/mcp`-URL.</p>
                </article>
                <article class="card">
                  <h2>Details</h2>
                  <div id="detailPanel" class="detail">Waehle Diagnose oder Discovery auf einem Modul aus.</div>
                </article>
              </aside>
              <section id="moduleGrid" class="grid"></section>
            </section>
          </main>
          <script>
            const statusNode = document.getElementById("status");
            const moduleGrid = document.getElementById("moduleGrid");
            const refreshButton = document.getElementById("refreshButton");
            const overviewNode = document.getElementById("overview");
            const summaryBar = document.getElementById("summaryBar");
            const detailPanel = document.getElementById("detailPanel");
            const createForm = document.getElementById("createForm");
            const newNetboxButton = document.getElementById("newNetboxButton");

            function esc(value) {{
              return String(value ?? "")
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;")
                .replaceAll("\\"", "&quot;")
                .replaceAll("'", "&#39;");
            }}

            function pretty(value) {{
              return JSON.stringify(value ?? {{}}, null, 2);
            }}

            function parseJson(text, fallback) {{
              const raw = String(text ?? "").trim();
              if (!raw) return fallback;
              return JSON.parse(raw);
            }}

            function modulePayloadFromForm(form) {{
              const data = new FormData(form);
              return {{
                id: String(data.get("id") || "").trim(),
                name: String(data.get("name") || "").trim(),
                type: String(data.get("type") || "docs").trim(),
                enabled: data.get("enabled") === "on",
                provider: String(data.get("provider") || "").trim(),
                transport: String(data.get("transport") || "local").trim(),
                remote_protocol: String(data.get("remote_protocol") || "auto").trim(),
                path: String(data.get("path") || "").trim(),
                base_url: String(data.get("base_url") || "").trim(),
                host: String(data.get("host") || "127.0.0.1").trim(),
                port: Number(data.get("port") || 0),
                timeout_seconds: Number(data.get("timeout_seconds") || 30),
                top_k: Number(data.get("top_k") || 5),
                api_key_env: String(data.get("api_key_env") || "").trim(),
                tool_names: String(data.get("tool_names") || "").split(",").map((item) => item.trim()).filter(Boolean),
                test_action: String(data.get("test_action") || "").trim(),
                test_payload: parseJson(data.get("test_payload") || "{{}}", {{}}),
                test_expect_contains: String(data.get("test_expect_contains") || "").split(",").map((item) => item.trim()).filter(Boolean),
                settings: parseJson(data.get("settings") || "{{}}", {{}}),
                notes: String(data.get("notes") || "").trim(),
                sources: []
              }};
            }}

            function populateForm(form, module) {{
              form.querySelector('[name="id"]').value = module.id || "";
              form.querySelector('[name="name"]').value = module.name || "";
              form.querySelector('[name="type"]').value = module.type || "docs";
              form.querySelector('[name="provider"]').value = module.provider || "";
              form.querySelector('[name="transport"]').value = module.transport || "local";
              form.querySelector('[name="remote_protocol"]').value = module.remote_protocol || "auto";
              form.querySelector('[name="path"]').value = module.path || "";
              form.querySelector('[name="base_url"]').value = module.base_url || "";
              form.querySelector('[name="host"]').value = module.host || "127.0.0.1";
              form.querySelector('[name="port"]').value = module.port || 0;
              form.querySelector('[name="timeout_seconds"]').value = module.timeout_seconds || 30;
              form.querySelector('[name="top_k"]').value = module.top_k || 5;
              form.querySelector('[name="api_key_env"]').value = module.api_key_env || "";
              form.querySelector('[name="tool_names"]').value = (module.tool_names || []).join(",");
              form.querySelector('[name="test_action"]').value = module.test_action || "";
              form.querySelector('[name="test_payload"]').value = pretty(module.test_payload || {{}});
              form.querySelector('[name="test_expect_contains"]').value = (module.test_expect_contains || []).join(",");
              form.querySelector('[name="settings"]').value = pretty(module.settings || {{}});
              form.querySelector('[name="notes"]').value = module.notes || "";
              form.querySelector('[name="enabled"]').checked = Boolean(module.enabled);
            }}

            function renderOverview(modules) {{
              const enabledModules = modules.filter((item) => item.enabled);
              const plannedOffline = modules.filter((item) => !item.enabled);
              const runtimeFailures = enabledModules.filter((item) => item.transport === "local" && !item.running);
              const discoveryFailures = enabledModules.filter((item) => item.transport === "remote" && item.runtime_state?.last_discovery_at && !item.runtime_state?.last_discovery_ok);
              const testFailures = enabledModules.filter((item) => item.runtime_state?.last_test_at && !item.runtime_state?.last_test_ok);
              const neverTested = enabledModules.filter((item) => !item.runtime_state?.last_test_at);
              const summary = {{
                total: modules.length,
                enabled: enabledModules.length,
                planned_offline: plannedOffline.length,
                local: modules.filter((item) => item.transport === "local").length,
                remote: modules.filter((item) => item.transport === "remote").length,
                running: enabledModules.filter((item) => item.running).length,
                invalid: enabledModules.filter((item) => item.validation_errors?.length).length,
                netbox: modules.filter((item) => item.provider === "netbox-mcp-server").length
              }};
              overviewNode.innerHTML = `
                <article class="metric"><span>Module gesamt</span><strong>${{summary.total}}</strong></article>
                <article class="metric"><span>Aktiv geplant</span><strong>${{summary.enabled}}</strong></article>
                <article class="metric"><span>Geplant offline</span><strong>${{summary.planned_offline}}</strong></article>
                <article class="metric"><span>Lokal</span><strong>${{summary.local}}</strong></article>
                <article class="metric"><span>Remote/MCP</span><strong>${{summary.remote}}</strong></article>
                <article class="metric"><span>Running</span><strong>${{summary.running}}</strong></article>
                <article class="metric"><span>Invalid</span><strong>${{summary.invalid}}</strong></article>
                <article class="metric"><span>NetBox MCP</span><strong>${{summary.netbox}}</strong></article>
              `;
              const pills = [];
              if (!enabledModules.length) {{
                pills.push('<span class="summary-pill info">Keine aktiven Module konfiguriert</span>');
              }}
              if (plannedOffline.length) {{
                pills.push(`<span class="summary-pill info">${{plannedOffline.length}} geplant offline</span>`);
              }}
              if (runtimeFailures.length) {{
                pills.push(`<span class="summary-pill fail">${{runtimeFailures.length}} aktive lokale Module ohne Runtime</span>`);
              }}
              if (discoveryFailures.length) {{
                pills.push(`<span class="summary-pill fail">${{discoveryFailures.length}} aktive Remote-Module mit fehlgeschlagener Discovery</span>`);
              }}
              if (testFailures.length) {{
                pills.push(`<span class="summary-pill fail">${{testFailures.length}} aktive Module mit fehlgeschlagenem Test</span>`);
              }}
              if (neverTested.length) {{
                pills.push(`<span class="summary-pill warn">${{neverTested.length}} aktive Module noch nicht getestet</span>`);
              }}
              if (!runtimeFailures.length && !discoveryFailures.length && !testFailures.length && enabledModules.length) {{
                pills.push('<span class="summary-pill ok">Keine aktiven Stoerungen im Runtime/Discovery/Test-Ueberblick</span>');
              }}
              summaryBar.innerHTML = pills.join("");
            }}

            function renderModules(modules) {{
              renderOverview(modules);
              if (!modules.length) {{
                moduleGrid.innerHTML = '<article class="card"><p>Keine Module konfiguriert.</p></article>';
                return;
              }}
              moduleGrid.innerHTML = modules.map((module) => {{
                const errors = module.validation_errors?.length
                  ? `<ul class="errors">${{module.validation_errors.map((item) => `<li>${{esc(item)}}</li>`).join("")}}</ul>`
                  : "";
                const endpoint = esc(module.endpoint || module.status?.base_url || module.status?.path || "");
                const state = module.runtime_state || {{}};
                const rawModule = esc(JSON.stringify(module.status || {{}}));
                const runtimeSignal = module.running ? "ok" : (module.transport === "remote" ? "warn" : "fail");
                const discoverySignal = module.transport === "remote"
                  ? (state.last_discovery_ok ? "ok" : (state.last_discovery_at ? "fail" : "warn"))
                  : "warn";
                const testSignal = state.last_test_ok
                  ? "ok"
                  : (state.last_test_at ? (state.last_test_connected ? "warn" : "fail") : "warn");
                return `
                  <article class="card" data-module-id="${{esc(module.id)}}">
                    <div class="card-head">
                      <div>
                        <h2>${{esc(module.name)}}</h2>
                        <div class="meta">${{esc(module.id)}} · ${{esc(module.type)}} · ${{esc(module.transport)}} · ${{esc(module.provider || "-")}}</div>
                      </div>
                      <span class="badge ${{esc(module.tone)}}">${{esc(module.state)}}</span>
                    </div>
                    <div class="signals">
                      <span class="signal ${{runtimeSignal}}"><span class="signal-dot"></span>Runtime</span>
                      <span class="signal ${{discoverySignal}}"><span class="signal-dot"></span>Discovery</span>
                      <span class="signal ${{testSignal}}"><span class="signal-dot"></span>Test</span>
                    </div>
                    <dl>
                      <dt>Enabled</dt><dd>${{module.enabled ? "yes" : "no"}}</dd>
                      <dt>Running</dt><dd>${{module.running ? "yes" : "no"}}</dd>
                      <dt>Protocol</dt><dd>${{esc(module.status?.remote_protocol || "auto")}}</dd>
                      <dt>Sources</dt><dd>${{esc(module.enabled_source_count)}} / ${{esc(module.source_count)}}</dd>
                      <dt>Endpoint</dt><dd>${{endpoint}}</dd>
                      <dt>Last Start</dt><dd>${{esc(state.last_started_at || "-")}}</dd>
                      <dt>Last Discovery</dt><dd>${{esc(state.last_discovery_at || "-")}}</dd>
                      <dt>Last Test</dt><dd>${{esc(state.last_test_at || "-")}}</dd>
                      <dt>Last Error</dt><dd>${{esc(state.last_error || "-")}}</dd>
                    </dl>
                    ${{errors}}
                    <div class="actions">
                      <button type="button" data-action="start">Start</button>
                      <button type="button" data-action="stop">Stop</button>
                      <button type="button" data-action="restart">Restart</button>
                      <button type="button" data-action="test">Test</button>
                      <button type="button" data-action="discover">Discover</button>
                      <button type="button" data-action="diagnose">Diagnose</button>
                      <button type="button" data-action="edit" class="secondary">Edit</button>
                      <button type="button" data-action="delete">Delete</button>
                    </div>
                    <section>
                      <h3>Konfiguration</h3>
                      <form class="module-edit form-grid">
                        <input type="hidden" name="id" value="${{esc(module.id)}}">
                        <div class="field"><label>Name</label><input name="name" value="${{esc(module.name || "")}}"></div>
                        <div class="field"><label>Provider</label><input name="provider" value="${{esc(module.status?.provider || "")}}"></div>
                        <div class="field"><label>Type</label><input name="type" value="${{esc(module.type)}}"></div>
                        <div class="field"><label>Transport</label><input name="transport" value="${{esc(module.transport)}}"></div>
                        <div class="field"><label>Remote Protocol</label><input name="remote_protocol" value="${{esc(module.status?.remote_protocol || "auto")}}"></div>
                        <div class="field"><label>Pfad</label><input name="path" value="${{esc(module.status?.path || "")}}"></div>
                        <div class="field"><label>Base URL</label><input name="base_url" value="${{esc(module.status?.base_url || "")}}"></div>
                        <div class="field"><label>Host</label><input name="host" value="${{esc(module.status?.host || "127.0.0.1")}}"></div>
                        <div class="field"><label>Port</label><input name="port" type="number" value="${{esc(module.status?.port || 0)}}"></div>
                        <div class="field"><label>Timeout</label><input name="timeout_seconds" type="number" step="0.1" value="${{esc(module.status?.timeout_seconds || 30)}}"></div>
                        <div class="field"><label>Top K</label><input name="top_k" type="number" value="${{esc(module.status?.top_k || 5)}}"></div>
                        <div class="field"><label>API key env</label><input name="api_key_env" value="${{esc(module.status?.api_key_env || "")}}"></div>
                        <div class="field"><label>Tool names</label><input name="tool_names" value="${{esc((module.status?.tool_names || []).join(","))}}"></div>
                        <div class="field"><label>Test Action</label><input name="test_action" value="${{esc(module.status?.test_action || "")}}"></div>
                        <div class="field"><label>Test Payload JSON</label><textarea name="test_payload">${{esc(JSON.stringify(module.status?.test_payload || {{}}, null, 2))}}</textarea></div>
                        <div class="field"><label>Erwartete Begriffe</label><input name="test_expect_contains" value="${{esc((module.status?.test_expect_contains || []).join(","))}}"></div>
                        <div class="field"><label>Settings JSON</label><textarea name="settings">${{esc(JSON.stringify(module.status?.settings || {{}}, null, 2))}}</textarea></div>
                        <div class="field"><label>Notes</label><textarea name="notes">${{esc(module.status?.notes || "")}}</textarea></div>
                        <div class="field"><label><input type="checkbox" name="enabled" ${{module.enabled ? "checked" : ""}}> aktiviert</label></div>
                        <div class="actions">
                          <button type="submit">Speichern</button>
                          <button type="button" data-action="show-raw" class="secondary">Raw</button>
                        </div>
                      </form>
                      <div class="detail" data-kind="raw" hidden>${{rawModule}}</div>
                    </section>
                  </article>
                `;
              }}).join("");
            }}

            async function loadModules() {{
              statusNode.textContent = "Lade Module...";
              try {{
                const response = await fetch("/api/modules/overview");
                const payload = await response.json().catch(() => ({{ detail: "Ungueltige Server-Antwort." }}));
                if (!response.ok) {{
                  throw new Error(payload.detail || "Modul-Overview konnte nicht geladen werden.");
                }}
                renderModules(payload.modules || []);
                statusNode.textContent = `Stand: ${{new Date().toLocaleTimeString("de-DE")}}`;
              }} catch (error) {{
                statusNode.textContent = error.message;
                moduleGrid.innerHTML = `<article class="card"><p>${{esc(error.message)}}</p></article>`;
              }}
            }}

            async function createModule(payload) {{
              statusNode.textContent = `Erzeuge ${{payload.id}}...`;
              const response = await fetch("/api/modules", {{
                method: "POST",
                headers: {{ "Content-Type": "application/json" }},
                body: JSON.stringify(payload)
              }});
              const result = await response.json().catch(() => ({{ detail: "Ungueltige Server-Antwort." }}));
              if (!response.ok) {{
                throw new Error(result.detail || "Modul konnte nicht angelegt werden.");
              }}
              statusNode.textContent = result.message || "Modul gespeichert.";
              await loadModules();
            }}

            async function updateModule(moduleId, payload) {{
              statusNode.textContent = `Speichere ${{moduleId}}...`;
              const response = await fetch(`/api/modules/${{encodeURIComponent(moduleId)}}`, {{
                method: "PUT",
                headers: {{ "Content-Type": "application/json" }},
                body: JSON.stringify(payload)
              }});
              const result = await response.json().catch(() => ({{ detail: "Ungueltige Server-Antwort." }}));
              if (!response.ok) {{
                throw new Error(result.detail || "Modul konnte nicht aktualisiert werden.");
              }}
              statusNode.textContent = result.message || "Modul aktualisiert.";
              await loadModules();
            }}

            async function runAction(moduleId, action) {{
              statusNode.textContent = `${{moduleId}}: ${{action}}...`;
              try {{
                let response;
                if (action === "discover") {{
                  response = await fetch(`/api/modules/${{encodeURIComponent(moduleId)}}/discover`, {{ method: "POST" }});
                }} else if (action === "diagnose") {{
                  response = await fetch(`/api/modules/${{encodeURIComponent(moduleId)}}/diagnose`);
                }} else if (action === "test") {{
                  response = await fetch(`/api/modules/${{encodeURIComponent(moduleId)}}/test`, {{ method: "POST" }});
                }} else if (action === "delete") {{
                  response = await fetch(`/api/modules/${{encodeURIComponent(moduleId)}}`, {{ method: "DELETE" }});
                }} else {{
                  response = await fetch(`/api/modules/${{encodeURIComponent(moduleId)}}/${{action}}`, {{ method: "POST" }});
                }}
                const payload = await response.json().catch(() => ({{ detail: "Ungueltige Server-Antwort." }}));
                if (!response.ok) {{
                  throw new Error(payload.detail || `Aktion ${{action}} fehlgeschlagen.`);
                }}
                if (action === "discover" || action === "diagnose" || action === "test") {{
                  detailPanel.textContent = pretty(payload);
                }}
                statusNode.textContent = payload.message || `${{moduleId}}: ${{action}} ok`;
                await loadModules();
              }} catch (error) {{
                statusNode.textContent = error.message;
              }}
            }}

            createForm.addEventListener("submit", async (event) => {{
              event.preventDefault();
              try {{
                await createModule(modulePayloadFromForm(createForm));
                createForm.reset();
                document.getElementById("create-test-payload").value = "{{}}";
                document.getElementById("create-settings").value = "{{}}";
              }} catch (error) {{
                statusNode.textContent = error.message;
              }}
            }});

            newNetboxButton.addEventListener("click", () => {{
              populateForm(createForm, {{
                id: "netbox",
                name: "NetBox MCP",
                type: "mcp_http",
                provider: "netbox-mcp-server",
                transport: "remote",
                remote_protocol: "mcp",
                base_url: "http://127.0.0.1:8000/mcp",
                host: "127.0.0.1",
                port: 0,
                timeout_seconds: 30,
                top_k: 5,
                tool_names: ["get_objects", "get_object_by_id", "get_changelogs"],
                test_action: "discover",
                test_payload: {{}},
                test_expect_contains: ["get_objects"],
                settings: {{
                  netbox_url: "",
                  netbox_token_env: "NETBOX_TOKEN",
                  verify_ssl: true,
                  upstream_repo: "https://github.com/netboxlabs/netbox-mcp-server"
                }},
                notes: "HTTP MCP endpoint des netbox-mcp-server auf /mcp",
                enabled: true
              }});
            }});

            refreshButton.addEventListener("click", loadModules);
            moduleGrid.addEventListener("click", (event) => {{
              const button = event.target.closest("button[data-action]");
              if (!button) return;
              const card = button.closest("[data-module-id]");
              if (!card) return;
              if (button.dataset.action === "edit") {{
                const form = card.querySelector("form.module-edit");
                if (form) {{
                  form.scrollIntoView({{ behavior: "smooth", block: "center" }});
                }}
                return;
              }}
              if (button.dataset.action === "show-raw") {{
                const raw = card.querySelector('[data-kind="raw"]');
                if (raw) {{
                  raw.hidden = !raw.hidden;
                }}
                return;
              }}
              runAction(card.dataset.moduleId, button.dataset.action);
            }});

            moduleGrid.addEventListener("submit", async (event) => {{
              const form = event.target.closest("form.module-edit");
              if (!form) return;
              event.preventDefault();
              const payload = modulePayloadFromForm(form);
              try {{
                await updateModule(payload.id, payload);
              }} catch (error) {{
                statusNode.textContent = error.message;
              }}
            }});

            loadModules();
          </script>
        </body>
        </html>
        """
    )


def _context_for_chat(message: str, selected_modules: list[str] | None) -> tuple[list[dict[str, Any]], list[str]]:
    selected = set(selected_modules or [])
    snippets: list[dict[str, Any]] = []
    used_modules: list[str] = []
    for module in load_modules():
        if not module.enabled:
            continue
        if selected and module.id not in selected:
            continue
        if module.type not in {"docs", "maildir"}:
            continue
        try:
            result = execute_module(module.id, "search", {"query": message, "top_k": module.top_k})
        except Exception:
            continue
        hits = result.get("data", {}).get("hits", [])
        if not hits:
            continue
        snippets.append({"module": module.id, "hits": hits[:3]})
        used_modules.append(module.id)
    return snippets, used_modules


def _build_messages(settings: HarborSettings, message: str, selected_modules: list[str] | None) -> tuple[list[dict[str, str]], list[str]]:
    context, used_modules = _context_for_chat(message, selected_modules)
    prompt_parts = [system_prompt(settings)]
    if context:
        prompt_parts.append("Kontext aus lokalen Modulen:")
        prompt_parts.append(json.dumps(context, ensure_ascii=False, indent=2))
    prompt_parts.append("Antworte knapp, direkt und auf Basis des bereitgestellten Kontexts.")
    return (
        [
            {"role": "system", "content": "\n\n".join(prompt_parts)},
            {"role": "user", "content": message},
        ],
        used_modules,
    )


def _request_to_module(body: ModuleUpsertRequest) -> ModuleConfig:
    sources = [
        ModuleSource(id=item.id.strip(), path=item.path.strip(), label=item.label.strip(), enabled=item.enabled)
        for item in body.sources
        if item.path.strip()
    ]
    return ModuleConfig(
        id=body.id.strip(),
        type=str(body.type).strip(),
        enabled=body.enabled,
        name=body.name.strip(),
        provider=body.provider.strip(),
        transport=body.transport.strip(),
        remote_protocol=body.remote_protocol.strip(),
        path=body.path.strip(),
        base_url=body.base_url.strip(),
        api_key=body.api_key,
        api_key_env=body.api_key_env.strip(),
        host=body.host.strip(),
        port=body.port,
        timeout_seconds=body.timeout_seconds,
        top_k=body.top_k,
        notes=body.notes,
        tool_names=[item.strip() for item in body.tool_names if item.strip()],
        test_action=body.test_action.strip(),
        test_payload=body.test_payload,
        test_expect_contains=[item.strip() for item in body.test_expect_contains if item.strip()],
        settings=body.settings,
        sources=sources,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="Harbor", version="0.1.0")

    @app.get("/", response_class=RedirectResponse)
    def home() -> RedirectResponse:
        return RedirectResponse(url="/chat")

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(_user=require_role("viewer")) -> HTMLResponse:
        return HTMLResponse(_chat_page_html(load_settings()))

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(_user=require_role("admin")) -> HTMLResponse:
        return HTMLResponse(_admin_page_html(load_settings()))

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        settings = load_settings()
        return {
            "ok": True,
            "name": settings.name,
            "host": settings.host,
            "port": settings.port,
            "modules": len(load_modules()),
        }

    @app.get("/api/modules")
    def modules(_user= require_role("viewer")) -> dict[str, Any]:
        return {"modules": [module_status(module) for module in load_modules()]}

    @app.get("/api/modules/overview")
    def modules_overview(_user=require_role("admin")) -> dict[str, Any]:
        return {"modules": list_module_overview()}

    @app.post("/api/modules")
    def module_create(body: ModuleUpsertRequest, _user=require_role("operator")) -> dict[str, Any]:
        try:
            module = _request_to_module(body)
            upsert_module(module)
            return {"ok": True, "message": f"Modul gespeichert: {module.id}", "status": module_status(module)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/modules/{module_id}")
    def module_update(module_id: str, body: ModuleUpsertRequest, _user=require_role("operator")) -> dict[str, Any]:
        if body.id != module_id:
            raise HTTPException(status_code=400, detail="Pfad-ID und Body-ID stimmen nicht ueberein.")
        try:
            module = _request_to_module(body)
            upsert_module(module)
            return {"ok": True, "message": f"Modul aktualisiert: {module.id}", "status": module_status(module)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/modules/{module_id}")
    def module_delete(module_id: str, _user=require_role("operator")) -> dict[str, Any]:
        try:
            try:
                stop_module(module_id)
            except Exception:
                pass
            removed = remove_module(module_id)
            if not removed:
                raise HTTPException(status_code=404, detail="Modul nicht gefunden.")
            return {"ok": True, "message": f"Modul entfernt: {module_id}"}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/modules/{module_id}/start")
    def module_start(module_id: str, _user=require_role("operator")) -> dict[str, Any]:
        try:
            return start_module(module_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/modules/{module_id}/stop")
    def module_stop(module_id: str, _user=require_role("operator")) -> dict[str, Any]:
        try:
            return stop_module(module_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/modules/{module_id}/restart")
    def module_restart(module_id: str, _user=require_role("operator")) -> dict[str, Any]:
        try:
            return restart_module(module_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/modules/{module_id}/execute")
    def module_execute(module_id: str, body: ExecuteRequest, _user=require_role("operator")) -> dict[str, Any]:
        try:
            return execute_module(module_id, body.action, body.payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/modules/{module_id}/discover")
    def module_discover(module_id: str, _user=require_role("operator")) -> dict[str, Any]:
        module = find_module(module_id)
        if module is None:
            raise HTTPException(status_code=404, detail="Modul nicht gefunden.")
        try:
            return discover_remote_module(module)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/modules/{module_id}/test")
    def module_run_test(module_id: str, _user=require_role("operator")) -> dict[str, Any]:
        try:
            return module_test(module_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/modules/{module_id}/diagnose")
    def module_diagnose(module_id: str, _user=require_role("admin")) -> dict[str, Any]:
        try:
            return module_diagnostics(module_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/modules/{module_id}/logs")
    def module_logs(module_id: str, lines: int = 50, _user=require_role("admin")) -> dict[str, Any]:
        path = module_log_path(module_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Logdatei nicht gefunden.")
        entries = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"ok": True, "module_id": module_id, "log_path": str(path), "lines": entries[-lines:]}

    @app.post("/api/chat")
    def chat(body: ChatRequest, _user=require_role("viewer")) -> dict[str, Any]:
        settings = load_settings()
        messages, used_modules = _build_messages(settings, body.message, body.modules)
        try:
            response = complete_chat(settings, messages)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        content = extract_chat_content(response)
        return {"ok": True, "reply": content, "used_modules": used_modules, "raw": response}

    @app.get("/api/modules/{module_id}")
    def module_get(module_id: str, _user=require_role("viewer")) -> dict[str, Any]:
        module = find_module(module_id)
        if module is None:
            raise HTTPException(status_code=404, detail="Modul nicht gefunden.")
        return module_status(module)

    return app
