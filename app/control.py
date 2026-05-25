from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import HarborSettings, find_module, load_modules, load_settings, system_prompt
from .llm import complete_chat
from .modules import execute_module, module_status, restart_module, start_module, stop_module


class ExecuteRequest(BaseModel):
    action: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    modules: list[str] | None = None


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
    def modules() -> dict[str, Any]:
        return {"modules": [module_status(module) for module in load_modules()]}

    @app.post("/api/modules/{module_id}/start")
    def module_start(module_id: str) -> dict[str, Any]:
        try:
            return start_module(module_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/modules/{module_id}/stop")
    def module_stop(module_id: str) -> dict[str, Any]:
        try:
            return stop_module(module_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/modules/{module_id}/restart")
    def module_restart(module_id: str) -> dict[str, Any]:
        try:
            return restart_module(module_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/modules/{module_id}/execute")
    def module_execute(module_id: str, body: ExecuteRequest) -> dict[str, Any]:
        try:
            return execute_module(module_id, body.action, body.payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/chat")
    def chat(body: ChatRequest) -> dict[str, Any]:
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
    def module_get(module_id: str) -> dict[str, Any]:
        module = find_module(module_id)
        if module is None:
            raise HTTPException(status_code=404, detail="Modul nicht gefunden.")
        return module_status(module)

    return app
