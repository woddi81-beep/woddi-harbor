from __future__ import annotations

import getpass
import json
import os
import secrets
import shutil
import sys
from dataclasses import replace
from typing import Optional

import typer
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .auth import hash_password
from .backup import create_backup, restore_backup
from .config import (
    PID_DIR,
    SECRETS_DIR,
    HarborUser,
    ModuleConfig,
    delete_module_named_secret,
    ensure_layout,
    find_module,
    load_module_named_secret,
    load_modules,
    load_settings,
    load_users,
    parse_user_role,
    save_module_named_secret,
    save_settings,
    save_users,
    sync_service_profiles,
)
from .console import run_console
from .jobs import run_job_worker
from .llm import llm_health
from .mcp_lifecycle import (
    create_instance,
    install_package,
    lifecycle_overview,
    restart_instance,
    rollback_instance,
    start_instance,
    stop_instance,
    upgrade_instance,
)
from .modules import (
    discover_remote_module,
    execute_module,
    health_check_module,
    module_diagnostics,
    module_log_path,
    module_status,
    module_test,
    parse_json_payload,
    remove_module,
    reserve_port,
    restart_module,
    start_module,
    stop_module,
    upsert_module,
    validate_module_config,
    validation_errors_by_module,
)
from .preflight import production_check
from .runtime import restart_all, start_all, stop_all, uninstall_runtime
from .services import health_check_service, install_and_optionally_enable_service, service_action
from .sources import configure_document_sources, source_overview, sync_source
from .version import __version__
from .worker import run_worker

app = typer.Typer(
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
    help="woddi-harbor control hub. Ohne Unterbefehl startet die interaktive Konsole.",
)
module_app = typer.Typer(no_args_is_help=True)
llm_app = typer.Typer(no_args_is_help=True)
service_app = typer.Typer(no_args_is_help=True)
user_app = typer.Typer(no_args_is_help=True)
mcp_app = typer.Typer(no_args_is_help=True)
backup_app = typer.Typer(no_args_is_help=True)
source_app = typer.Typer(no_args_is_help=True)
runtime_app = typer.Typer(no_args_is_help=True)
server_app = typer.Typer(no_args_is_help=True)
app.add_typer(module_app, name="module")
app.add_typer(llm_app, name="llm")
app.add_typer(service_app, name="service")
app.add_typer(user_app, name="user")
app.add_typer(mcp_app, name="mcp")
app.add_typer(backup_app, name="backup")
app.add_typer(source_app, name="source")
app.add_typer(runtime_app, name="runtime")
app.add_typer(server_app, name="server")
console = Console()


def _open_console(*, simple: bool = False) -> None:
    ensure_layout()
    if simple:
        run_console(console)
        return
    from .tui import run_tui

    run_tui()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"woddi-harbor {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed woddi-harbor version and exit.",
    ),
) -> None:
    """Open the interactive console when no command is supplied."""
    del version
    if ctx.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        console.print("Interaktive Konsole benötigt ein Terminal. Nutze `woddi-harbor --help` für Automatisierung.")
        raise typer.Exit(code=2)
    _open_console()


def _check(name: str, ok: bool, detail: str) -> None:
    status = "[green]pass[/green]" if ok else "[red]fail[/red]"
    console.print(f"{status} {name}: {detail}")


@app.command("version")
def version_command(short: bool = typer.Option(False, "--short", help="Print only the semantic version.")) -> None:
    """Show the installed woddi-harbor version."""
    typer.echo(__version__ if short else f"woddi-harbor {__version__}")


def _print_modules() -> None:
    table = Table(title="Harbor Modules")
    table.add_column("ID")
    table.add_column("Provider")
    table.add_column("Type")
    table.add_column("Transport")
    table.add_column("Protocol")
    table.add_column("State")
    table.add_column("Endpoint / Path")
    for module in load_modules():
        status = module_status(module)
        endpoint = module.base_url or module.path or f"http://{module.host}:{module.port}"
        table.add_row(
            module.id,
            module.provider or "-",
            module.type,
            module.transport,
            module.remote_protocol,
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


@mcp_app.command("list")
def mcp_list() -> None:
    console.print_json(json.dumps(lifecycle_overview(), ensure_ascii=False))


@mcp_app.command("install")
def mcp_install(source: str = typer.Argument(...)) -> None:
    console.print_json(json.dumps(install_package(source, actor="cli"), ensure_ascii=False))


@mcp_app.command("create")
def mcp_create(
    instance_id: str = typer.Argument(...),
    package_id: str = typer.Option(...),
    version: str = typer.Option(...),
    config_json: str = typer.Option("{}"),
) -> None:
    config = parse_json_payload(config_json)
    console.print_json(
        json.dumps(create_instance(instance_id, package_id, version, config, actor="cli"), ensure_ascii=False)
    )


@mcp_app.command("start")
def mcp_start(instance_id: str) -> None:
    console.print_json(json.dumps(start_instance(instance_id, actor="cli"), ensure_ascii=False))


@mcp_app.command("stop")
def mcp_stop(instance_id: str) -> None:
    console.print_json(json.dumps(stop_instance(instance_id, actor="cli"), ensure_ascii=False))


@mcp_app.command("restart")
def mcp_restart(instance_id: str) -> None:
    console.print_json(json.dumps(restart_instance(instance_id, actor="cli"), ensure_ascii=False))


@mcp_app.command("upgrade")
def mcp_upgrade(instance_id: str, version: str = typer.Option(...)) -> None:
    console.print_json(json.dumps(upgrade_instance(instance_id, version, actor="cli"), ensure_ascii=False))


@mcp_app.command("rollback")
def mcp_rollback(instance_id: str) -> None:
    console.print_json(json.dumps(rollback_instance(instance_id, actor="cli"), ensure_ascii=False))


@backup_app.command("create")
def backup_create(label: str = typer.Option("manual")) -> None:
    console.print(str(create_backup(label)))


@backup_app.command("restore")
def backup_restore(source: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes")) -> None:
    if not yes:
        raise typer.BadParameter("Restore ist destruktiv. Bestaetige explizit mit --yes.")
    safety_backup = restore_backup(source)
    console.print(f"Restore abgeschlossen. Safety-Backup: {safety_backup}")


@source_app.command("list")
def source_list() -> None:
    console.print_json(json.dumps({"sources": source_overview()}, ensure_ascii=False))


@source_app.command("sync")
def source_sync(source_id: str, reindex: bool = typer.Option(True, "--reindex/--no-reindex")) -> None:
    console.print_json(json.dumps(sync_source(source_id, reindex=reindex), ensure_ascii=False))


@source_app.command("configure-docs")
def source_configure_docs(
    operations_path: str = typer.Option(
        "/opt/woddi-ai/doku/documentation-operation-main",
        "--operations-path",
    ),
    customer_path: str = typer.Option(
        "/opt/woddi-ai/doku/documentation-customer-main",
        "--customer-path",
    ),
) -> None:
    """Configure the production Markdown repositories as local document sources."""
    result = configure_document_sources(operations_path, customer_path)
    console.print_json(json.dumps(result, ensure_ascii=False))


@runtime_app.command("stop-all")
def runtime_stop_all() -> None:
    """Stop all Harbor services, modules, MCP processes and monitoring."""
    result = stop_all()
    console.print_json(json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        raise typer.Exit(code=2)


@runtime_app.command("start-all")
def runtime_start_all() -> None:
    """Start Harbor and all enabled local MCP workers."""
    result = start_all()
    console.print_json(json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        raise typer.Exit(code=2)


@runtime_app.command("restart-all")
def runtime_restart_all() -> None:
    """Restart Harbor and all enabled local MCP workers."""
    result = restart_all()
    console.print_json(json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        raise typer.Exit(code=2)


@runtime_app.command("uninstall")
def runtime_uninstall(yes: bool = typer.Option(False, "--yes", help="Confirm removal of managed runtime services.")) -> None:
    """Stop and remove Harbor runtime services while preserving all data."""
    if not yes:
        raise typer.BadParameter("Bestaetige das Entfernen der Runtime-Dienste mit --yes.")
    result = uninstall_runtime()
    console.print_json(json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        raise typer.Exit(code=2)


@app.command()
def onboard(
    llm_base_url: str = typer.Option("", help="OpenAI-compatible /v1 base URL"),
    llm_model: str = typer.Option("", help="Default model name"),
    llm_api_key_env: str = typer.Option("HARBOR_LLM_API_KEY"),
    docs_path: str = typer.Option("", help="Optional first docs path"),
    maildir_path: str = typer.Option("", help="Optional first maildir path"),
    mcp_base_url: str = typer.Option("", help="Optional first MCP HTTP URL"),
) -> None:
    """Guided first-run onboarding without the interactive console."""
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
    console.print(Panel.fit("Onboarding gespeichert. Danach `woddi-harbor console` starten.", title="Onboard"))


@app.command("init-admin")
def init_admin(
    username: str = typer.Option("admin"),
    role: str = typer.Option("admin"),
    generate: bool = typer.Option(False, "--generate", help="Generate a strong bootstrap password"),
) -> None:
    """Create the first local admin user."""
    ensure_layout()
    try:
        parsed_role = parse_user_role(role)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if load_users():
        raise typer.BadParameter("Benutzer existieren bereits. Nutze `woddi-harbor user add`.")
    if generate:
        password = secrets.token_urlsafe(32)
    else:
        password = getpass.getpass("Password: ")
        password_confirm = getpass.getpass("Confirm Password: ")
        if password != password_confirm:
            raise typer.BadParameter("Passwoerter stimmen nicht ueberein.")
    save_users([HarborUser(username=username, password_hash=hash_password(password), role=parsed_role, enabled=True)])
    if generate:
        password_path = SECRETS_DIR / "bootstrap-admin-password"
        password_path.write_text(password + "\n", encoding="utf-8")
        password_path.chmod(0o600)
        console.print(
            Panel.fit(
                f"Initialer Benutzer angelegt: {username}\nBootstrap-Passwort: {password_path}",
                title="Auth",
            )
        )
    else:
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


@app.command("production-check")
def production_check_command() -> None:
    """Run the production readiness gate."""
    result = production_check()
    console.print_json(json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        raise typer.Exit(code=2)


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


@app.command("console")
def console_command(
    simple: bool = typer.Option(False, "--simple", help="Use the line-oriented fallback console."),
) -> None:
    """Open the interactive Harbor console."""
    _open_console(simple=simple)


@app.command(hidden=True)
def tui() -> None:
    """Compatibility alias for `woddi-harbor console`."""
    _open_console()


@app.command("console-ui", hidden=True)
def console_ui() -> None:
    """Compatibility alias for `woddi-harbor console --simple`."""
    _open_console(simple=True)


@app.command()
def serve(host: Optional[str] = None, port: Optional[int] = None) -> None:
    """Run the Harbor control API."""
    settings = load_settings()
    PID_DIR.mkdir(parents=True, exist_ok=True)
    pid_path = PID_DIR / "harbor.pid"
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        uvicorn.run(
            "app.control:create_app",
            factory=True,
            host=host or settings.host,
            port=port or settings.port,
            workers=settings.api_workers,
        )
    finally:
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass


@server_app.command("set")
def server_set(
    host: str = typer.Option(..., "--host", help="Listen address, e.g. 0.0.0.0 for all IPv4 interfaces."),
    port: int = typer.Option(9680, "--port", min=1, max=65535),
) -> None:
    """Persist the Harbor listen address and port."""
    normalized_host = host.strip()
    if not normalized_host:
        raise typer.BadParameter("Host darf nicht leer sein.")
    settings = load_settings()
    settings.host = normalized_host
    settings.port = port
    settings.listen_configured = True
    save_settings(settings)
    console.print_json(
        json.dumps(
            {
                "ok": True,
                "host": settings.host,
                "port": settings.port,
                "external": settings.host not in {"127.0.0.1", "::1", "localhost"},
            },
            ensure_ascii=False,
        )
    )


@server_app.command("show")
def server_show() -> None:
    """Show the configured Harbor listen address and port."""
    settings = load_settings()
    console.print_json(json.dumps({"host": settings.host, "port": settings.port}, ensure_ascii=False))


@app.command()
def chat(message: str, modules: str = "") -> None:
    """Send a chat request directly through the configured LLM."""
    from .control import _build_messages
    from .llm import complete_chat, extract_chat_content

    settings = load_settings()
    selected_modules = [item.strip() for item in modules.split(",") if item.strip()]
    llm_messages, used_modules = _build_messages(settings, message, selected_modules or None)
    response = complete_chat(settings, llm_messages)
    reply = extract_chat_content(response)
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


@llm_app.command("check")
def llm_check() -> None:
    """Check LLM reachability and configured model availability."""
    result = llm_health(load_settings())
    console.print_json(json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        raise typer.Exit(code=2)


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
        test_action="stats",
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
        test_action="stats",
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
    provider: str = typer.Option("generic"),
    api_key: str = typer.Option(""),
    api_key_env: str = typer.Option(""),
    timeout_seconds: float = typer.Option(30.0),
    remote_protocol: str = typer.Option("auto"),
) -> None:
    """Register an external MCP-style HTTP service."""
    module = ModuleConfig(
        id=module_id,
        name=name,
        type="mcp_http",
        provider=provider,
        transport="remote",
        remote_protocol=remote_protocol,
        base_url=base_url,
        api_key=api_key,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
        test_action="discover" if remote_protocol == "mcp" else "health",
    )
    errors = validate_module_config(module)
    if errors:
        raise typer.BadParameter(" ".join(errors))
    upsert_module(module)
    console.print(Panel.fit(f"MCP-Modul registriert: {module_id}", title="Module"))


@module_app.command("add-netbox-mcp")
def module_add_netbox_mcp(
    module_id: str = typer.Argument("netbox"),
    name: str = typer.Option("NetBox MCP"),
    netbox_url: str = typer.Option(..., help="Basis-URL der NetBox-Instanz, z. B. https://netbox.example.com"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(0, help="Optionaler lokaler Port; Standard ist dynamisch"),
    timeout_seconds: float = typer.Option(30.0),
) -> None:
    """Register an anonymous read-only NetBox MCP server managed by Harbor."""
    module = ModuleConfig(
        id=module_id,
        name=name,
        type="netbox_mcp",
        provider="netbox",
        transport="local",
        remote_protocol="mcp",
        host=host,
        port=port,
        timeout_seconds=timeout_seconds,
        tool_names=[
            "discover_object_types",
            "describe_object_type",
            "get_inventory_statistics",
            "get_objects",
            "get_object_by_id",
            "get_changelogs",
            "call_endpoint",
        ],
        test_action="discover",
        settings={
            "netbox_url": netbox_url,
            "upstream_repo": "https://github.com/netboxlabs/netbox-mcp-server",
        },
        notes="Harbor startet den anonymen, strikt read-only NetBox MCP Worker.",
    )
    errors = validate_module_config(module)
    if errors:
        raise typer.BadParameter(" ".join(errors))
    upsert_module(module)
    delete_module_named_secret(module_id, "netbox_token")
    console.print(Panel.fit(f"NetBox MCP-Modul registriert: {module_id}", title="Module"))


@module_app.command("add-sap-docs-mcp")
def module_add_sap_docs_mcp(
    module_id: str = typer.Argument("sap_docs"),
    name: str = typer.Option("SAP Docs MCP"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(0, help="Optionaler lokaler Port; Standard ist dynamisch"),
    timeout_seconds: float = typer.Option(30.0),
    docs_url: str = typer.Option(..., help="SAP Help Dokumentations-URL"),
) -> None:
    """Register a local SAP documentation MCP server managed by Harbor."""
    module = ModuleConfig(
        id=module_id,
        name=name,
        type="sap_docs_mcp",
        provider="sap-docs-mcp-server",
        transport="local",
        remote_protocol="mcp",
        host=host,
        port=port,
        timeout_seconds=timeout_seconds,
        tool_names=["search_sap_docs"],
        test_action="discover",
        test_expect_contains=["search_sap_docs"],
        settings={"docs_url": docs_url},
        notes="Harbor startet den lokalen SAP Docs MCP Worker und exponiert /mcp sowie /health.",
    )
    errors = validate_module_config(module)
    if errors:
        raise typer.BadParameter(" ".join(errors))
    upsert_module(module)
    console.print(Panel.fit(f"SAP Docs MCP-Modul registriert: {module_id}", title="Module"))


@module_app.command("add-openstack-mcp")
def module_add_openstack_mcp(
    module_id: str = typer.Argument("openstack"),
    base_url: str = typer.Option(..., help="HTTP MCP Endpoint, z. B. http://127.0.0.1:8080/mcp"),
    name: str = typer.Option("OpenStack MCP"),
    api_key: str = typer.Option("", help="Optionaler Bearer Token fuer den MCP Endpoint"),
    api_key_env: str = typer.Option("", help="ENV-Name fuer den MCP Bearer Token"),
    timeout_seconds: float = typer.Option(30.0),
) -> None:
    """Register an external OpenStack MCP endpoint."""
    module = ModuleConfig(
        id=module_id,
        name=name,
        type="mcp_http",
        provider="openstack-mcp-server",
        transport="remote",
        remote_protocol="mcp",
        base_url=base_url,
        api_key=api_key,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
        tool_names=[
            "discover_resources",
            "get_storage_statistics",
            "get_project_statistics",
            "list_servers",
            "list_projects",
            "list_images",
        ],
        test_action="discover",
        test_payload={},
        test_expect_contains=["list_servers"],
        settings={"upstream_repo": "https://github.com/call518/MCP-OpenStack-Ops"},
        notes="Remote MCP endpoint fuer einen OpenStack MCP Server.",
    )
    errors = validate_module_config(module)
    if errors:
        raise typer.BadParameter(" ".join(errors))
    upsert_module(module)
    console.print(Panel.fit(f"OpenStack MCP-Modul registriert: {module_id}", title="Module"))


@module_app.command("add-openstack-local-mcp")
def module_add_openstack_local_mcp(
    module_id: str = typer.Argument("openstack"),
    name: str = typer.Option("OpenStack MCP"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(0, help="Optionaler lokaler Port; Standard ist dynamisch"),
    timeout_seconds: float = typer.Option(30.0),
    auth_url: str = typer.Option("", help="OpenStack Auth URL"),
    auth_url_env: str = typer.Option("OS_AUTH_URL"),
    region_name: str = typer.Option("", help="OpenStack Region"),
    region_name_env: str = typer.Option("OS_REGION_NAME"),
    token: str = typer.Option("", help="Projektgescoptes OpenStack User-Token"),
    token_env: str = typer.Option("OS_TOKEN", help="ENV-Name fuer das User-Token"),
) -> None:
    """Register a token-only local OpenStack MCP server managed by Harbor."""
    token_value = token if isinstance(token, str) else ""
    module = ModuleConfig(
        id=module_id,
        name=name,
        type="openstack_mcp",
        provider="openstack-mcp-server",
        transport="local",
        remote_protocol="mcp",
        host=host,
        port=port,
        timeout_seconds=timeout_seconds,
        tool_names=[
            "discover_resources",
            "get_storage_statistics",
            "get_project_statistics",
            "list_servers",
            "list_projects",
            "list_images",
            "list_flavors",
            "list_networks",
        ],
        test_action="discover",
        settings={
            "auth_type": "token",
            "auth_url": auth_url,
            "auth_url_env": auth_url_env,
            "region_name": region_name,
            "region_name_env": region_name_env,
            "token_env": token_env,
            "upstream_repo": "https://github.com/call518/MCP-OpenStack-Ops",
        },
        notes="Harbor nutzt ausschliesslich den Projektkontext des projektgescopten User-Tokens.",
    )
    errors = validate_module_config(module)
    if errors:
        raise typer.BadParameter(" ".join(errors))
    old_secret = load_module_named_secret(module_id, "openstack_token")
    try:
        if token_value:
            save_module_named_secret(module_id, "openstack_token", token_value)
        upsert_module(module)
        delete_module_named_secret(module_id, "openstack_application_credential_secret")
        delete_module_named_secret(module_id, "openstack_password")
    except Exception:
        if token_value:
            if old_secret:
                save_module_named_secret(module_id, "openstack_token", old_secret)
            else:
                delete_module_named_secret(module_id, "openstack_token")
        raise
    console.print(Panel.fit(f"Lokales OpenStack MCP-Modul registriert: {module_id}", title="Module"))


@module_app.command("set")
def module_set(
    module_id: str,
    name: str = typer.Option("", help="Neuer Anzeigename"),
    enabled: Optional[bool] = typer.Option(None, "--enabled/--disabled"),
    provider: str = typer.Option("", help="Provider, z. B. netbox-mcp-server"),
    base_url: str = typer.Option("", help="Remote URL"),
    path: str = typer.Option("", help="Lokaler Pfad"),
    host: str = typer.Option("", help="Lokaler Host"),
    port: int = typer.Option(-1, help="Lokaler Port"),
    top_k: int = typer.Option(-1, help="Top-K fuer Suchmodule"),
    timeout_seconds: float = typer.Option(-1.0),
    api_key: str = typer.Option(""),
    api_key_env: str = typer.Option(""),
    remote_protocol: str = typer.Option("", help="auto|harbor_execute|mcp"),
    notes: str = typer.Option("", help="Notizen"),
    tool_names: str = typer.Option("", help="Kommagetrennte Tool-Namen"),
    test_action: str = typer.Option("", help="Probe-Aktion fuer module test"),
    test_payload: str = typer.Option("", help="Probe-Payload als JSON-Objekt"),
    test_expect_contains: str = typer.Option("", help="Kommagetrennte Begriffe, die in der Ausgabe vorkommen sollen"),
    settings_json: str = typer.Option("", help="Zusatzkonfiguration als JSON-Objekt"),
) -> None:
    """Update an existing module or addon configuration."""
    module = find_module(module_id)
    if module is None:
        raise typer.BadParameter(f"Modul nicht gefunden: {module_id}")
    updated = replace(module)
    if name:
        updated.name = name.strip()
    if enabled is not None:
        updated.enabled = enabled
    if provider:
        updated.provider = provider.strip()
    if base_url:
        updated.base_url = base_url.strip()
    if path:
        updated.path = path.strip()
        if len(updated.sources) == 1:
            updated.sources[0].path = updated.path
    if host:
        updated.host = host.strip()
    if port >= 0:
        updated.port = port
    if top_k > 0:
        updated.top_k = top_k
    if timeout_seconds >= 0:
        updated.timeout_seconds = timeout_seconds
    if api_key:
        updated.api_key = api_key
    if api_key_env:
        updated.api_key_env = api_key_env.strip()
    if remote_protocol:
        updated.remote_protocol = remote_protocol.strip()
    if notes:
        updated.notes = notes
    if tool_names:
        updated.tool_names = [item.strip() for item in tool_names.split(",") if item.strip()]
    if test_action:
        updated.test_action = test_action.strip()
    if test_payload:
        updated.test_payload = parse_json_payload(test_payload)
    if test_expect_contains:
        updated.test_expect_contains = [item.strip() for item in test_expect_contains.split(",") if item.strip()]
    if settings_json:
        parsed = parse_json_payload(settings_json)
        updated.settings = parsed
    errors = validation_errors_by_module([candidate if candidate.id != module_id else updated for candidate in load_modules()]).get(module_id, [])
    if errors:
        raise typer.BadParameter(" ".join(errors))
    upsert_module(updated)
    console.print(Panel.fit(f"Modul aktualisiert: {module_id}", title="Module"))


@module_app.command("remove")
def module_remove(module_id: str) -> None:
    """Remove a module or addon configuration."""
    try:
        stop_module(module_id)
    except Exception:
        pass
    removed = remove_module(module_id)
    if not removed:
        raise typer.BadParameter(f"Modul nicht gefunden: {module_id}")
    console.print(Panel.fit(f"Modul entfernt: {module_id}", title="Module"))


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
def module_call(
    module_id: str,
    action: str,
    payload: str = typer.Argument("{}", help="JSON-Payload als Positionsargument"),
    payload_option: Optional[str] = typer.Option(None, "--payload", help="JSON-Payload als Option"),
) -> None:
    """Call a module action."""
    try:
        effective_payload = payload_option if payload_option is not None else payload
        result = execute_module(module_id, action, parse_json_payload(effective_payload))
    except Exception as exc:
        console.print(f"[red]Modulaufruf fehlgeschlagen:[/red] {exc}")
        raise typer.Exit(code=2) from exc
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
    """Run capability discovery for a remote or local MCP module."""
    module = find_module(module_id)
    if module is None:
        raise typer.BadParameter(f"Modul nicht gefunden: {module_id}")
    if module.type not in {"mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"}:
        raise typer.BadParameter("discover ist nur fuer MCP-Module sinnvoll.")
    try:
        result = discover_remote_module(module)
    except Exception as exc:
        raise typer.BadParameter(f"Discovery fehlgeschlagen: {exc}") from exc
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Module Discovery"))


@module_app.command("diagnose")
def module_diagnose(module_id: str, lines: int = 40) -> None:
    """Show combined status, health, discovery and recent logs for a module."""
    try:
        result = module_diagnostics(module_id, log_lines=lines)
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Module Diagnose"))


@module_app.command("test")
def module_run_test(module_id: str) -> None:
    """Run the configured connectivity and output smoke test for a module."""
    result = module_test(module_id)
    console.print(Panel.fit(json.dumps(result, ensure_ascii=False, indent=2), title="Module Test"))


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
    parsed_role = parse_user_role(role)
    users = load_users()
    if any(user.username == username for user in users):
        raise typer.BadParameter(f"Benutzer existiert bereits: {username}")
    password = getpass.getpass("Password: ")
    password_confirm = getpass.getpass("Confirm Password: ")
    if password != password_confirm:
        raise typer.BadParameter("Passwoerter stimmen nicht ueberein.")
    users.append(HarborUser(username=username, password_hash=hash_password(password), role=parsed_role, enabled=True))
    save_users(users)
    console.print(Panel.fit(f"Benutzer angelegt: {username}", title="User"))


@user_app.command("set-role")
def user_set_role(username: str, role: str) -> None:
    """Change a user's role."""
    if role not in {"admin", "operator", "viewer"}:
        raise typer.BadParameter("role muss admin, operator oder viewer sein.")
    parsed_role = parse_user_role(role)
    users = load_users()
    changed = False
    for user in users:
        if user.username == username:
            user.role = parsed_role
            changed = True
            break
    if not changed:
        raise typer.BadParameter(f"Benutzer nicht gefunden: {username}")
    save_users(users)
    console.print(Panel.fit(f"Rolle gesetzt: {username} -> {role}", title="User"))


@user_app.command("passwd")
def user_password(username: str) -> None:
    """Change a local user's password."""
    users = load_users()
    user = next((item for item in users if item.username == username), None)
    if user is None:
        raise typer.BadParameter(f"Benutzer nicht gefunden: {username}")
    password = getpass.getpass("New password: ")
    password_confirm = getpass.getpass("Confirm password: ")
    if password != password_confirm:
        raise typer.BadParameter("Passwoerter stimmen nicht ueberein.")
    user.password_hash = hash_password(password)
    save_users(users)
    bootstrap_path = SECRETS_DIR / "bootstrap-admin-password"
    bootstrap_path.unlink(missing_ok=True)
    console.print(Panel.fit(f"Passwort aktualisiert: {username}", title="User"))


@user_app.command("set-permissions")
def user_set_permissions(
    username: str,
    modules: str = typer.Option("*", help="Kommagetrennte Modul-IDs oder *"),
    tools: str = typer.Option("*", help="Kommagetrennte Tool-Namen oder *"),
) -> None:
    """Set per-user module and tool allowlists."""
    users = load_users()
    changed = False
    for user in users:
        if user.username == username:
            user.allowed_modules = [item.strip() for item in modules.split(",") if item.strip()]
            user.allowed_tools = [item.strip() for item in tools.split(",") if item.strip()]
            changed = True
            break
    if not changed:
        raise typer.BadParameter(f"Benutzer nicht gefunden: {username}")
    save_users(users)
    console.print(Panel.fit(f"Berechtigungen aktualisiert: {username}", title="User"))


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
    try:
        run_worker(module_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("job-worker")
def job_worker(once: bool = typer.Option(False, help="Process at most one queued job.")) -> None:
    """Run the durable background job worker."""
    run_job_worker(once=once)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
