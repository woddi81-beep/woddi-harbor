from __future__ import annotations

import json
from dataclasses import replace

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from .config import ModuleConfig, load_modules, load_settings, parse_module_type, save_settings, save_system_prompt, system_prompt
from .llm import complete_chat
from .modules import (
    execute_module,
    module_log_path,
    module_status,
    parse_json_payload,
    remove_module,
    reserve_port,
    restart_module,
    start_module,
    stop_module,
    upsert_module,
)


def run_console(console: Console) -> None:
    while True:
        _render_dashboard(console)
        choice = Prompt.ask(
            "[bold #ffd166]Aktion[/bold #ffd166]",
            choices=["chat", "llm", "module", "prompt", "server", "refresh", "quit"],
            default="refresh",
            show_choices=True,
        )
        if choice == "chat":
            _chat_loop(console)
        elif choice == "llm":
            _llm_wizard(console)
        elif choice == "module":
            _module_hub(console)
        elif choice == "prompt":
            _system_prompt_editor(console)
        elif choice == "server":
            _server_settings(console)
        elif choice == "refresh":
            continue
        elif choice == "quit":
            break


def _render_dashboard(console: Console) -> None:
    console.clear()
    settings = load_settings()
    modules = load_modules()

    title = Text("woddi-harbor control console", style="bold #00d1b2")
    shell = Panel.fit(
        f"[bold]LLM[/bold]\n{settings.llm.model or '-'}\n{settings.llm.base_url or '-'}",
        title="Brain",
        border_style="#00d1b2",
    )
    server = Panel.fit(
        f"[bold]API[/bold]\n{settings.host}:{settings.port}\n[dim]CLI-first local control[/dim]",
        title="Bridge",
        border_style="#ff7b72",
    )
    prompt_panel = Panel.fit(
        _truncate(system_prompt(settings), 160),
        title="System Prompt",
        border_style="#7aa2f7",
    )
    console.print(title)
    console.print(Columns([shell, server, prompt_panel], equal=True, expand=True))
    console.print(_module_table(modules))
    console.print(
        Panel.fit(
            "chat  llm  module  prompt  server  refresh  quit",
            title="Command Deck",
            border_style="#9ece6a",
        )
    )


def _module_table(modules: list[ModuleConfig]) -> Table:
    table = Table(title="Services and Modules", box=box.SIMPLE_HEAVY)
    table.add_column("ID", style="bold")
    table.add_column("Type")
    table.add_column("Mode")
    table.add_column("State")
    table.add_column("Endpoint / Path")
    if not modules:
        table.add_row("-", "-", "-", "empty", "Noch keine Module registriert")
        return table
    for module in modules:
        status = module_status(module)
        endpoint = module.base_url or module.path or f"http://{module.host}:{module.port}"
        state = "[green]running[/green]" if status["running"] else "[yellow]stopped[/yellow]"
        if module.transport == "remote":
            state = "[cyan]remote[/cyan]"
        table.add_row(module.id, module.type, module.transport, state, endpoint)
    return table


def _llm_wizard(console: Console) -> None:
    settings = load_settings()
    console.print(Panel.fit("LLM configuration", border_style="#00d1b2"))
    base_url = Prompt.ask("Base URL", default=settings.llm.base_url or "http://127.0.0.1:8000/v1")
    model = Prompt.ask("Model", default=settings.llm.model or "gpt-4.1")
    api_key_env = Prompt.ask("API key env var", default=settings.llm.api_key_env or "HARBOR_LLM_API_KEY")
    timeout_seconds = float(Prompt.ask("Timeout Sekunden", default=str(settings.llm.timeout_seconds)))
    max_tokens = IntPrompt.ask("Max Tokens", default=settings.llm.max_tokens)
    updated = replace(
        settings,
        llm=replace(
            settings.llm,
            base_url=base_url.strip(),
            model=model.strip(),
            api_key_env=api_key_env.strip(),
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        ),
    )
    save_settings(updated)
    console.print("[green]LLM-Konfiguration gespeichert.[/green]")


def _module_hub(console: Console) -> None:
    while True:
        modules = load_modules()
        console.print(_module_table(modules))
        choice = Prompt.ask(
            "Module action",
            choices=["add", "manage", "remove", "back"],
            default="back",
        )
        if choice == "add":
            _module_add_wizard(console)
        elif choice == "manage":
            _module_manage_wizard(console)
        elif choice == "remove":
            _module_remove_wizard(console)
        elif choice == "back":
            return


def _module_add_wizard(console: Console) -> None:
    kind = Prompt.ask("Neuer Modultyp", choices=["docs", "maildir", "mcp_http"])
    module_id = Prompt.ask("Module ID").strip()
    name = Prompt.ask("Anzeigename", default=module_id).strip()
    if kind in {"docs", "maildir"}:
        path = Prompt.ask("Pfad").strip()
        port = IntPrompt.ask("Port", default=reserve_port())
        top_k = IntPrompt.ask("Top K", default=5)
        module = ModuleConfig(
            id=module_id,
            name=name,
            type=parse_module_type(kind),
            transport="local",
            path=path,
            port=port,
            top_k=top_k,
        )
    else:
        base_url = Prompt.ask("Base URL", default="http://127.0.0.1:9010").strip()
        api_key_env = Prompt.ask("API key env var", default="").strip()
        timeout_seconds = float(Prompt.ask("Timeout Sekunden", default="30"))
        module = ModuleConfig(
            id=module_id,
            name=name,
            type="mcp_http",
            transport="remote",
            base_url=base_url,
            api_key_env=api_key_env,
            timeout_seconds=timeout_seconds,
        )
    upsert_module(module)
    console.print(f"[green]Modul gespeichert:[/green] {module_id}")
    if module.transport == "local" and Confirm.ask("Direkt starten?", default=True):
        result = start_module(module.id)
        console.print_json(json.dumps(result, ensure_ascii=False))


def _pick_module_id(console: Console) -> str | None:
    modules = load_modules()
    if not modules:
        console.print("[yellow]Keine Module vorhanden.[/yellow]")
        return None
    ids = [module.id for module in modules]
    console.print("Verfuegbare Module: " + ", ".join(ids))
    selected = Prompt.ask("Module ID").strip()
    if selected not in ids:
        console.print(f"[red]Unbekanntes Modul:[/red] {selected}")
        return None
    return selected


def _module_manage_wizard(console: Console) -> None:
    module_id = _pick_module_id(console)
    if not module_id:
        return
    while True:
        status = module_status(next(module for module in load_modules() if module.id == module_id))
        console.print(Panel(json.dumps(status, ensure_ascii=False, indent=2), title=f"Module {module_id}", border_style="#7aa2f7"))
        action = Prompt.ask(
            "Aktion",
            choices=["start", "stop", "restart", "call", "logs", "back"],
            default="back",
        )
        if action == "start":
            console.print_json(json.dumps(start_module(module_id), ensure_ascii=False))
        elif action == "stop":
            console.print_json(json.dumps(stop_module(module_id), ensure_ascii=False))
        elif action == "restart":
            console.print_json(json.dumps(restart_module(module_id), ensure_ascii=False))
        elif action == "call":
            module = next(module for module in load_modules() if module.id == module_id)
            default_action = "search" if module.type in {"docs", "maildir"} else "health"
            request_action = Prompt.ask("Action", default=default_action).strip()
            payload = Prompt.ask("Payload JSON", default='{"query": ""}' if default_action == "search" else "{}")
            result = execute_module(module_id, request_action, parse_json_payload(payload))
            console.print_json(json.dumps(result, ensure_ascii=False))
        elif action == "logs":
            path = module_log_path(module_id)
            if not path.exists():
                console.print("[yellow]Noch kein Log vorhanden.[/yellow]")
            else:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                console.print(Panel("\n".join(lines[-60:]), title=f"Logs {module_id}", border_style="#ff7b72"))
        elif action == "back":
            return


def _module_remove_wizard(console: Console) -> None:
    module_id = _pick_module_id(console)
    if not module_id:
        return
    if Confirm.ask(f"Modul {module_id} wirklich entfernen?", default=False):
        try:
            stop_module(module_id)
        except Exception:
            pass
        removed = remove_module(module_id)
        if removed:
            console.print(f"[green]Modul entfernt:[/green] {module_id}")
        else:
            console.print(f"[red]Modul nicht entfernt:[/red] {module_id}")


def _system_prompt_editor(console: Console) -> None:
    current = system_prompt()
    console.print(Panel(current, title="Aktueller System Prompt", border_style="#7aa2f7"))
    console.print("Neuen Prompt eingeben. Mit `END` beenden.")
    lines: list[str] = []
    while True:
        line = Prompt.ask("")
        if line == "END":
            break
        lines.append(line)
    new_text = "\n".join(lines).strip()
    if not new_text:
        console.print("[yellow]Keine Aenderung gespeichert.[/yellow]")
        return
    save_system_prompt(new_text)
    console.print("[green]System Prompt gespeichert.[/green]")


def _server_settings(console: Console) -> None:
    settings = load_settings()
    console.print(Panel.fit(f"Host: {settings.host}\nPort: {settings.port}", title="Server"))
    host = Prompt.ask("Host", default=settings.host).strip()
    port = IntPrompt.ask("Port", default=settings.port)
    save_settings(replace(settings, host=host, port=port))
    console.print("[green]Server-Einstellungen gespeichert.[/green]")


def _chat_loop(console: Console) -> None:
    settings = load_settings()
    if not settings.llm.base_url or not settings.llm.model:
        console.print("[red]LLM ist noch nicht konfiguriert.[/red]")
        return
    console.print(Panel.fit("Chat mit Harbor. Leere Eingabe beendet den Chat.", border_style="#00d1b2"))
    while True:
        message = Prompt.ask("[bold]Du[/bold]").strip()
        if not message:
            return
        selected = Prompt.ask("Module CSV oder leer fuer auto", default="").strip()
        selected_modules = [item.strip() for item in selected.split(",") if item.strip()]
        from .control import _build_messages

        messages, used_modules = _build_messages(settings, message, selected_modules or None)
        try:
            response = complete_chat(settings, messages)
            choices = response.get("choices") or []
            reply = str(choices[0].get("message", {}).get("content", "")) if choices else ""
            console.print(Panel(reply or "(leer)", title=f"Harbor | modules={','.join(used_modules) or '-'}", border_style="#9ece6a"))
        except Exception as exc:
            console.print(f"[red]Chat fehlgeschlagen:[/red] {exc}")


def _truncate(text: str, limit: int) -> str:
    stripped = " ".join(text.split())
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3] + "..."
