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
from .config import HarborSettings, LOG_DIR, find_module, load_modules, load_settings, system_prompt
from .llm import complete_chat
from .modules import execute_module, list_module_overview, module_status, restart_module, start_module, stop_module


APP_STARTED_AT = time.time()
RECENT_ACTIVITY: deque[dict[str, Any]] = deque(maxlen=25)
DEFAULT_LOG_PATH = Path("~/.harbor/logs/harbor.log").expanduser()


class ExecuteRequest(BaseModel):
    action: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    modules: list[str] | None = None


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
              --bg: #f3f5f7;
              --panel: #ffffff;
              --line: #d7dde3;
              --text: #1d2733;
              --muted: #617182;
              --success: #1d6b44;
              --warning: #946200;
              --danger: #a12f2f;
              --info: #235f9a;
              --disabled: #6b7280;
            }}
            * {{ box-sizing: border-box; }}
            body {{
              margin: 0;
              font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
              color: var(--text);
              background:
                linear-gradient(180deg, rgba(16, 82, 121, 0.08), transparent 22%),
                linear-gradient(180deg, #f8fafc 0%, var(--bg) 100%);
            }}
            .shell {{
              max-width: 1200px;
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
          </style>
        </head>
        <body>
          <main class="shell">
            <header class="topbar">
              <div>
                <h1>{title}</h1>
                <p>Admin-Ansicht fuer Module und Prozesssteuerung</p>
              </div>
              <a href="/chat">Chat</a>
            </header>
            <div class="toolbar">
              <button id="refreshButton" type="button">Aktualisieren</button>
              <div id="status"></div>
            </div>
            <section id="moduleGrid" class="grid"></section>
          </main>
          <script>
            const statusNode = document.getElementById("status");
            const moduleGrid = document.getElementById("moduleGrid");
            const refreshButton = document.getElementById("refreshButton");

            function esc(value) {{
              return String(value ?? "")
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;")
                .replaceAll("\\"", "&quot;")
                .replaceAll("'", "&#39;");
            }}

            function renderModules(modules) {{
              if (!modules.length) {{
                moduleGrid.innerHTML = '<article class="card"><p>Keine Module konfiguriert.</p></article>';
                return;
              }}
              moduleGrid.innerHTML = modules.map((module) => {{
                const errors = module.validation_errors?.length
                  ? `<ul class="errors">${{module.validation_errors.map((item) => `<li>${{esc(item)}}</li>`).join("")}}</ul>`
                  : "";
                const endpoint = esc(module.endpoint || module.status?.base_url || module.status?.path || "");
                return `
                  <article class="card" data-module-id="${{esc(module.id)}}">
                    <div class="card-head">
                      <div>
                        <h2>${{esc(module.name)}}</h2>
                        <div class="meta">${{esc(module.id)}} · ${{esc(module.type)}} · ${{esc(module.transport)}}</div>
                      </div>
                      <span class="badge ${{esc(module.tone)}}">${{esc(module.state)}}</span>
                    </div>
                    <dl>
                      <dt>Enabled</dt><dd>${{module.enabled ? "yes" : "no"}}</dd>
                      <dt>Running</dt><dd>${{module.running ? "yes" : "no"}}</dd>
                      <dt>Sources</dt><dd>${{esc(module.enabled_source_count)}} / ${{esc(module.source_count)}}</dd>
                      <dt>Endpoint</dt><dd>${{endpoint}}</dd>
                    </dl>
                    ${{errors}}
                    <div class="actions">
                      <button type="button" data-action="start">Start</button>
                      <button type="button" data-action="stop">Stop</button>
                      <button type="button" data-action="restart">Restart</button>
                    </div>
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

            async function runAction(moduleId, action) {{
              statusNode.textContent = `${{moduleId}}: ${{action}}...`;
              try {{
                const response = await fetch(`/api/modules/${{encodeURIComponent(moduleId)}}/${{action}}`, {{
                  method: "POST"
                }});
                const payload = await response.json().catch(() => ({{ detail: "Ungueltige Server-Antwort." }}));
                if (!response.ok) {{
                  throw new Error(payload.detail || `Aktion ${{action}} fehlgeschlagen.`);
                }}
                statusNode.textContent = payload.message || `${{moduleId}}: ${{action}} ok`;
                await loadModules();
              }} catch (error) {{
                statusNode.textContent = error.message;
              }}
            }}

            refreshButton.addEventListener("click", loadModules);
            moduleGrid.addEventListener("click", (event) => {{
              const button = event.target.closest("button[data-action]");
              if (!button) return;
              const card = button.closest("[data-module-id]");
              if (!card) return;
              runAction(card.dataset.moduleId, button.dataset.action);
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

    @app.post("/api/chat")
    def chat(body: ChatRequest, _user=require_role("viewer")) -> dict[str, Any]:
        settings = load_settings()
        messages, used_modules = _build_messages(settings, body.message, body.modules)
        try:
            response = complete_chat(settings, messages)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        content = ""
        choices = response.get("choices") or []
        if choices:
            content = str(choices[0].get("message", {}).get("content", ""))
        return {"ok": True, "reply": content, "used_modules": used_modules, "raw": response}

    @app.get("/api/modules/{module_id}")
    def module_get(module_id: str, _user=require_role("viewer")) -> dict[str, Any]:
        module = find_module(module_id)
        if module is None:
            raise HTTPException(status_code=404, detail="Modul nicht gefunden.")
        return module_status(module)

    return app
