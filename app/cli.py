from __future__ import annotations

import json
import getpass
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

from .auth import hash_password
from .config import (
    HarborUser,
    HarborSettings,
    ModuleConfig,
    ensure_layout,
    find_module,
    load_modules,
    load_service_profiles,
    load_settings,
    load_users,
    save_users,
    save_settings,
    sync_service_profiles,
)
from .console import run_console
from .control import create_app
from .modules import (
    discover_remote_module,
    execute_module,
    health_check_module,
    module_log_path,
    module_status,
    parse_json_payload,
    restart_module,
    reserve_port,
    start_module,
    stop_module,
    upsert_module,
    validate_module_config,
    worker_execute,
)
from .services import health_check_service, install_and_optionally_enable_service, install_service, service_action


app = typer.Typer(no_args_is_help=True, add_completion=False)
module_app = typer.Typer(no_args_is_help=True)
llm_app = typer.Typer(no_args_is_help=True)
service_app = typer.Typer(no_args_is_help=True)
user_app = typer.Typer(no_args_is_help=True)
app.add_typer(module_app, name="module")
app.add_typer(llm_app, name="llm")
app.add_typer(service_app, name="service")
app.add_typer(user_app, name="user")
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


def _print_services() -> None:
    table = Table(title="Harbor Services")
    table.add_column("Profile")
    table.add_column("Kind")
    table.add_column("Mode")
    table.add_column("Unit")
    table.add_column("Autostart")
    for profile in sync_service_profiles():
        table.add_row(
            profile.id,
            profile.kind,
            profile.systemd_mode,
            profile.resolved_unit_name(),
            "yes" if profile.autostart else "no",
        )
    console.print(table)


def _print_users() -> None:
    table = Table(title="Harbor Users")
    table.add_column("Username")
    table.add_column("Role")
    table.add_column("Enabled")
    for user in load_users():
        table.add_row(user.username, user.role, "yes" if user.enabled else "no")
    console.print(table)


@app.command()
def init() -> None:
    """Create default configuration and directories."""
    ensure_layout()
    sync_service_profiles()
    console.print(Panel.fit("woddi-harbor initialisiert unter /srv/http/woddi-harbor", title="Ready"))


@app.command()
def onboard(
    llm_base_url: str = typer.Option("", help="OpenAI-compatible /v1 base URL"),
    llm_model: str = typer.Option("", help="Default model name"),
    llm_api_key_env: str = typer.Option("HARBOR_LLM_API_KEY"),
    docs_path: str = typer.Option("", help="Optional first docs path"),
    maildir_path: str = typer.Option("", help="Optional first maildir path"),
    mcp_base_url: str = typer.Option("", help="Optional first MCP HTTP URL"),
) -> None:
    """Guided first-run onboarding without the TUI."""
    ensure_layout()
    settings = load_settings()
    if llm_base_url:
        settings.llm.base_url = llm_base_url
    if llm_model:
        settings.llm.model = llm_model
    settings.llm.api_key_env = llm_api_key_env
    settings.onboarding_complete = True
    save_settings(settings)
    if docs_path:
        upsert_module(ModuleConfig(id="docs-local", type="docs", transport="local", path=docs_path, port=reserve_port()))
    if maildir_path:
        upsert_module(ModuleConfig(id="maildir-local", type="maildir", transport="local", path=maildir_path, port=reserve_port()))
    if mcp_base_url:
        upsert_module(ModuleConfig(id="mcp-remote", type="mcp_http", transport="remote", base_url=mcp_base_url))
    sync_service_profiles()
    console.print(Panel.fit("Onboarding gespeichert. Danach `./harbor.sh console` oder `woddi-harbor tui` starten.", title="Onboard"))


@app.command("init-admin")
def init_admin(
    username: str = typer.Option("admin"),
    role: str = typer.Option("admin"),
) -> None:
    """Create the first local admin user."""
    ensure_layout()
    if load_users():
        raise typer.BadParameter("Benutzer existieren bereits. Nutze `woddi-harbor user add`.")
    password = getpass.getpass("Password: ")
    password_confirm = getpass.getpass("Confirm Password: ")
    if password != password_confirm:
        raise typer.BadParameter("Passwoerter stimmen nicht ueberein.")
    save_users([HarborUser(username=username, password_hash=hash_password(password), role=role, enabled=True)])
    console.print(Panel.fit(f"Initialer Benutzer angelegt: {username}", title="Auth"))


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
    _print_services()


@app.command("console-ui")
def console_ui() -> None:
    """Open the interactive Harbor control console."""
    ensure_layout()
    run_console(console)


@app.command()
def tui() -> None:
    """Open the richer Harbor terminal UI."""
    ensure_layout()
    from .tui import run_tui

    run_tui()


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
    errors = validate_module_config(module)
    if errors:
        raise typer.BadParameter(" ".join(errors))
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
    errors = validate_module_config(module)
    if errors:
        raise typer.BadParameter(" ".join(errors))
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
    errors = validate_module_config(module)
    if errors:
        raise typer.BadParameter(" ".join(errors))
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


@module_app.command("check")
def module_check(module_id: str) -> None:
    """Validate and health-check a module."""
    result = health_check_module(module_id)
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Module Check"))


@module_app.command("reindex")
def module_reindex(module_id: str) -> None:
    """Force rebuild a local search index."""
    result = execute_module(module_id, "reindex", {})
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Module Reindex"))


@module_app.command("discover")
def module_discover(module_id: str) -> None:
    """Run remote capability discovery for an mcp_http module."""
    module = find_module(module_id)
    if module is None:
        raise typer.BadParameter(f"Modul nicht gefunden: {module_id}")
    if module.type != "mcp_http":
        raise typer.BadParameter("discover ist nur fuer mcp_http-Module sinnvoll.")
    result = discover_remote_module(module)
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Module Discovery"))


@service_app.command("list")
def service_list() -> None:
    """List service profiles."""
    sync_service_profiles()
    _print_services()


@service_app.command("install")
def service_install(
    profile_id: str = typer.Argument(..., help="harbor or module:<module-id>"),
    mode: str = typer.Option("user", help="user or system"),
    enable: bool = typer.Option(False, help="Enable after install"),
    start: bool = typer.Option(False, help="Start after install"),
) -> None:
    """Install a systemd service unit."""
    result = install_and_optionally_enable_service(profile_id, mode, enable=enable, start=start)
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Service Install"))


@service_app.command("run")
def service_run(
    profile_id: str = typer.Argument(..., help="harbor or module:<module-id>"),
    action: str = typer.Argument(..., help="start|stop|restart|enable|disable|status"),
) -> None:
    """Run a systemd action for an installed profile."""
    result = service_action(profile_id, action)
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Service Action"))


@service_app.command("check")
def service_check(profile_id: str = typer.Argument(..., help="harbor or module:<module-id>")) -> None:
    """Check installed systemd state for a profile."""
    result = health_check_service(profile_id)
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Service Check"))


@user_app.command("list")
def user_list() -> None:
    """List configured users."""
    _print_users()


@user_app.command("add")
def user_add(
    username: str,
    role: str = typer.Option("viewer"),
) -> None:
    """Add a new local user."""
    if role not in {"admin", "operator", "viewer"}:
        raise typer.BadParameter("role muss admin, operator oder viewer sein.")
    users = load_users()
    if any(user.username == username for user in users):
        raise typer.BadParameter(f"Benutzer existiert bereits: {username}")
    password = getpass.getpass("Password: ")
    password_confirm = getpass.getpass("Confirm Password: ")
    if password != password_confirm:
        raise typer.BadParameter("Passwoerter stimmen nicht ueberein.")
    users.append(HarborUser(username=username, password_hash=hash_password(password), role=role, enabled=True))
    save_users(users)
    console.print(Panel.fit(f"Benutzer angelegt: {username}", title="User"))


@user_app.command("set-role")
def user_set_role(username: str, role: str) -> None:
    """Change a user's role."""
    if role not in {"admin", "operator", "viewer"}:
        raise typer.BadParameter("role muss admin, operator oder viewer sein.")
    users = load_users()
    changed = False
    for user in users:
        if user.username == username:
            user.role = role
            changed = True
            break
    if not changed:
        raise typer.BadParameter(f"Benutzer nicht gefunden: {username}")
    save_users(users)
    console.print(Panel.fit(f"Rolle gesetzt: {username} -> {role}", title="User"))


@user_app.command("disable")
def user_disable(username: str) -> None:
    """Disable a user account."""
    users = load_users()
    changed = False
    for user in users:
        if user.username == username:
            user.enabled = False
            changed = True
            break
    if not changed:
        raise typer.BadParameter(f"Benutzer nicht gefunden: {username}")
    save_users(users)
    console.print(Panel.fit(f"Benutzer deaktiviert: {username}", title="User"))


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
