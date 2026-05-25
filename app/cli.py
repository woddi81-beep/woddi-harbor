from __future__ import annotations

import json
import shutil
import sys
from dataclasses import replace
from typing import Optional

import typer
import uvicorn
from fastapi import FastAPI
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import HarborSettings, ModuleConfig, ensure_layout, find_module, load_modules, load_settings, save_settings
from .console import run_console
from .control import create_app
from .modules import (
    execute_module,
    module_log_path,
    module_status,
    parse_json_payload,
    restart_module,
    reserve_port,
    start_module,
    stop_module,
    upsert_module,
    worker_execute,
)


app = typer.Typer(no_args_is_help=True, add_completion=False)
module_app = typer.Typer(no_args_is_help=True)
llm_app = typer.Typer(no_args_is_help=True)
app.add_typer(module_app, name="module")
app.add_typer(llm_app, name="llm")
console = Console()


def _check(name: str, ok: bool, detail: str) -> None:
    status = "[green]pass[/green]" if ok else "[red]fail[/red]"
    console.print(f"{status} {name}: {detail}")


def _print_modules() -> None:
    table = Table(title="Harbor Modules")
    table.add_column("ID")
    table.add_column("Type")
    table.add_column("Transport")
    table.add_column("State")
    table.add_column("Endpoint / Path")
    for module in load_modules():
        status = module_status(module)
        endpoint = module.base_url or module.path or f"http://{module.host}:{module.port}"
        table.add_row(
            module.id,
            module.type,
            module.transport,
            "running" if status["running"] else "stopped",
            endpoint,
        )
    console.print(table)


@app.command()
def init() -> None:
    """Create default configuration and directories."""
    ensure_layout()
    console.print(Panel.fit("woddi-harbor initialisiert unter /srv/http/woddi-harbor", title="Ready"))


@app.command("check-prerequisites")
def check_prerequisites() -> None:
    """Check basic Linux prerequisites for SLES/Ubuntu style deployments."""
    py_ok = sys.version_info >= (3, 10)
    venv_ok = True
    try:
        import venv  # noqa: F401
    except Exception:
        venv_ok = False
    _check("python", py_ok, f"{sys.version.split()[0]} (min 3.10)")
    _check("venv", venv_ok, "Python venv module available" if venv_ok else "python3-venv/python311-venv missing")
    _check("git", shutil.which("git") is not None, shutil.which("git") or "git missing")
    _check("systemctl", shutil.which("systemctl") is not None, shutil.which("systemctl") or "optional")
    _check("layout", True, "config/, data/, logs/ are created by `woddi-harbor init`")


@app.command()
def status() -> None:
    """Show Harbor configuration and module state."""
    settings = load_settings()
    console.print(
        Panel.fit(
            f"name={settings.name}\nlisten={settings.host}:{settings.port}\nllm={settings.llm.base_url or '-'}\nmodel={settings.llm.model or '-'}",
            title="Harbor",
        )
    )
    _print_modules()


@app.command("console-ui")
def console_ui() -> None:
    """Open the interactive Harbor control console."""
    ensure_layout()
    run_console(console)


@app.command()
def serve(host: Optional[str] = None, port: Optional[int] = None) -> None:
    """Run the Harbor control API."""
    settings = load_settings()
    api: FastAPI = create_app()
    uvicorn.run(api, host=host or settings.host, port=port or settings.port)


@app.command()
def chat(message: str, modules: str = "") -> None:
    """Send a chat request directly through the configured LLM."""
    from .control import _build_messages
    from .llm import complete_chat

    settings = load_settings()
    selected_modules = [item.strip() for item in modules.split(",") if item.strip()]
    llm_messages, used_modules = _build_messages(settings, message, selected_modules or None)
    response = complete_chat(settings, llm_messages)
    reply = ""
    choices = response.get("choices") or []
    if choices:
        reply = str(choices[0].get("message", {}).get("content", ""))
    console.print(Panel(reply or "(leer)", title=f"Harbor Reply | modules={','.join(used_modules) or '-'}"))


@llm_app.command("set")
def llm_set(
    base_url: str = typer.Option(...),
    model: str = typer.Option(...),
    api_key: str = typer.Option(""),
    api_key_env: str = typer.Option(""),
    timeout_seconds: float = typer.Option(60.0),
    max_tokens: int = typer.Option(1200),
) -> None:
    """Configure the external OpenAI-compatible LLM."""
    settings = load_settings()
    updated = replace(
        settings,
        llm=replace(
            settings.llm,
            base_url=base_url,
            model=model,
            api_key=api_key,
            api_key_env=api_key_env,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        ),
    )
    save_settings(updated)
    console.print(Panel.fit(f"LLM gesetzt: {model} @ {base_url}", title="LLM"))


@module_app.command("list")
def module_list() -> None:
    """List configured modules."""
    _print_modules()


@module_app.command("add-docs")
def module_add_docs(
    module_id: str,
    path: str,
    name: str = typer.Option(""),
    port: int = typer.Option(0),
    top_k: int = typer.Option(5),
) -> None:
    """Register a local documentation search module."""
    module = ModuleConfig(
        id=module_id,
        name=name,
        type="docs",
        transport="local",
        path=path,
        port=port or reserve_port(),
        top_k=top_k,
    )
    upsert_module(module)
    console.print(Panel.fit(f"Docs-Modul registriert: {module_id}", title="Module"))


@module_app.command("add-maildir")
def module_add_maildir(
    module_id: str,
    path: str,
    name: str = typer.Option(""),
    port: int = typer.Option(0),
    top_k: int = typer.Option(5),
) -> None:
    """Register a local mail search module."""
    module = ModuleConfig(
        id=module_id,
        name=name,
        type="maildir",
        transport="local",
        path=path,
        port=port or reserve_port(),
        top_k=top_k,
    )
    upsert_module(module)
    console.print(Panel.fit(f"Mail-Modul registriert: {module_id}", title="Module"))


@module_app.command("add-mcp")
def module_add_mcp(
    module_id: str,
    base_url: str,
    name: str = typer.Option(""),
    api_key: str = typer.Option(""),
    api_key_env: str = typer.Option(""),
    timeout_seconds: float = typer.Option(30.0),
) -> None:
    """Register an external MCP-style HTTP service."""
    module = ModuleConfig(
        id=module_id,
        name=name,
        type="mcp_http",
        transport="remote",
        base_url=base_url,
        api_key=api_key,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
    )
    upsert_module(module)
    console.print(Panel.fit(f"MCP-Modul registriert: {module_id}", title="Module"))


@module_app.command("start")
def module_start(module_id: str) -> None:
    """Start a local module process."""
    result = start_module(module_id)
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Start"))


@module_app.command("stop")
def module_stop(module_id: str) -> None:
    """Stop a local module process."""
    result = stop_module(module_id)
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Stop"))


@module_app.command("restart")
def module_restart(module_id: str) -> None:
    """Restart a local module process."""
    result = restart_module(module_id)
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Restart"))


@module_app.command("call")
def module_call(module_id: str, action: str, payload: str = "{}") -> None:
    """Call a module action."""
    result = execute_module(module_id, action, parse_json_payload(payload))
    console.print_json(json.dumps(result, ensure_ascii=False))


@module_app.command("logs")
def module_logs(module_id: str, lines: int = 50) -> None:
    """Show recent module logs."""
    log_path = module_log_path(module_id)
    if not log_path.exists():
        raise typer.BadParameter(f"Kein Log fuer {module_id}: {log_path}")
    text = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    console.print(Panel("\n".join(text[-lines:]), title=f"Logs {module_id}"))


@app.command(hidden=True)
def worker(module_id: str) -> None:
    """Run a single local module worker."""
    module = find_module(module_id)
    if module is None:
        raise typer.BadParameter(f"Modul nicht gefunden: {module_id}")
    api = FastAPI(title=f"Harbor Worker {module_id}")

    @api.get("/health")
    def health() -> dict:
        return module_status(module)

    @api.post("/execute")
    def execute(body: dict) -> dict:
        action = str(body.get("action", "")).strip()
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            raise typer.BadParameter("payload muss ein JSON-Objekt sein.")
        return worker_execute(module, action, payload)

    uvicorn.run(api, host=module.host, port=module.port, log_level="warning")


def main() -> None:
    app()
