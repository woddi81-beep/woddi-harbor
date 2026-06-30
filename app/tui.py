from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from rich.syntax import Syntax
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, RichLog, Static

from .config import (
    ModuleConfig,
    load_modules,
    load_settings,
    parse_module_type,
    save_settings,
    save_system_prompt,
    sync_service_profiles,
    system_prompt,
)
from .modules import (
    execute_module,
    health_check_module,
    module_log_path,
    module_status,
    parse_json_payload,
    remove_module,
    reserve_port,
    restart_module,
    start_module,
    stop_module,
    upsert_module,
    validate_module_config,
)
from .services import health_check_service, install_and_optionally_enable_service, service_action


class ModuleItem(ListItem):
    def __init__(self, module: ModuleConfig) -> None:
        self.module_id = module.id
        endpoint = module.base_url or module.path or f"http://{module.host}:{module.port}"
        label = Label(f"{module.id}\n[dim]{module.type} | {endpoint}[/dim]")
        super().__init__(label)


class DictFormScreen(ModalScreen[dict[str, str] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    DictFormScreen {
        align: center middle;
    }

    #dialog {
        width: 88;
        height: auto;
        max-height: 90%;
        border: round #00d1b2;
        background: #0d1117;
        padding: 1 2;
    }

    .form-label {
        margin-top: 1;
        color: #9ece6a;
    }

    .form-input {
        margin-bottom: 1;
    }

    #buttons {
        height: auto;
        align-horizontal: right;
        margin-top: 1;
    }

    Button {
        margin-left: 1;
    }
    """

    def __init__(self, title: str, fields: list[dict[str, str]], *, submit_label: str = "Save") -> None:
        super().__init__()
        self.title = title
        self.fields = fields
        self.submit_label = submit_label

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static(self.title or "", classes="title")
            for field in self.fields:
                yield Label(field["label"], classes="form-label")
                yield Input(
                    value=field.get("value", ""),
                    placeholder=field.get("placeholder", ""),
                    password=field.get("password", "false") == "true",
                    id=f"field-{field['name']}",
                    classes="form-input",
                )
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.submit_label, variant="success", id="submit")

    @on(Button.Pressed)
    def handle_buttons(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        values: dict[str, str] = {}
        for field in self.fields:
            widget = self.query_one(f"#field-{field['name']}", Input)
            values[field["name"]] = widget.value
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HarborTui(App[None]):
    CSS = """
    Screen {
        background: #0b1020;
        color: #e6edf3;
    }

    #root {
        height: 1fr;
    }

    #sidebar {
        width: 34;
        border-right: heavy #1f6feb;
        padding: 1;
        background: #0f172a;
    }

    #main {
        padding: 1;
    }

    #module-list {
        height: 1fr;
        border: round #334155;
        background: #111827;
    }

    .section-title {
        color: #9ece6a;
        margin-bottom: 1;
    }

    .card {
        width: 1fr;
        min-height: 7;
        border: round #334155;
        background: #111827;
        padding: 1;
        margin-right: 1;
    }

    #detail {
        height: 1fr;
        border: round #7aa2f7;
        background: #0f172a;
        padding: 1;
        margin-top: 1;
    }

    #event-log {
        height: 12;
        border: round #ff7b72;
        background: #111827;
        margin-top: 1;
    }

    #actions {
        height: auto;
        margin-top: 1;
        color: #94a3b8;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "add_module", "Add Module"),
        Binding("enter", "show_module", "Show"),
        Binding("s", "start_selected", "Start"),
        Binding("x", "stop_selected", "Stop"),
        Binding("d", "restart_selected", "Restart"),
        Binding("c", "call_selected", "Call"),
        Binding("g", "show_logs", "Logs"),
        Binding("backspace", "remove_selected", "Remove"),
        Binding("l", "configure_llm", "LLM"),
        Binding("p", "edit_prompt", "Prompt"),
        Binding("v", "edit_server", "Server"),
        Binding("u", "install_user_service", "Install Unit"),
        Binding("e", "enable_service", "Enable"),
        Binding("z", "service_status", "Service Status"),
        Binding("h", "health_check", "Health Check"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.selected_module_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="root"):
            with Vertical(id="sidebar"):
                yield Static("Service Deck", classes="section-title")
                yield ListView(id="module-list")
                yield Static(
                    "a add  s start  x stop  d restart  c call\nu install  e enable  z svc-status  h health\nl llm  p prompt  v server  g logs  q quit",
                    id="actions",
                )
            with Vertical(id="main"):
                with Horizontal():
                    yield Static(id="card-llm", classes="card")
                    yield Static(id="card-server", classes="card")
                    yield Static(id="card-prompt", classes="card")
                    yield Static(id="card-services", classes="card")
                yield Static(id="detail")
                yield RichLog(id="event-log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "woddi-harbor"
        self.sub_title = "Local AI control console"
        self.refresh_dashboard()
        self._write_event("Console ready. Create modules with [b]a[/b] or configure the LLM with [b]l[/b].")
        settings = load_settings()
        configured_installation = bool(settings.llm.base_url and settings.llm.model) or bool(load_modules())
        if not settings.onboarding_complete and not configured_installation:
            self.call_after_refresh(self.action_onboard)

    def refresh_dashboard(self) -> None:
        settings = load_settings()
        modules = load_modules()
        profiles = sync_service_profiles()

        list_view = self.query_one("#module-list", ListView)
        list_view.clear()
        for module in modules:
            list_view.append(ModuleItem(module))

        if modules and self.selected_module_id not in {module.id for module in modules}:
            self.selected_module_id = modules[0].id
        if modules and self.selected_module_id is None:
            self.selected_module_id = modules[0].id

        self.query_one("#card-llm", Static).update(
            f"[b]LLM[/b]\n{settings.llm.model or '-'}\n{settings.llm.base_url or '-'}"
        )
        self.query_one("#card-server", Static).update(
            f"[b]Server[/b]\n{settings.host}:{settings.port}\n{len(modules)} module"
        )
        self.query_one("#card-prompt", Static).update(
            f"[b]Prompt[/b]\n{_truncate(system_prompt(settings), 120)}"
        )
        user_units = sum(1 for item in profiles if item.systemd_mode == "user")
        system_units = sum(1 for item in profiles if item.systemd_mode == "system")
        self.query_one("#card-services", Static).update(
            f"[b]Services[/b]\nprofiles={len(profiles)}\nuser={user_units} system={system_units}"
        )
        self._render_detail()

    def _render_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        if not self.selected_module_id:
            detail.update("No module selected yet.")
            return
        module = next((item for item in load_modules() if item.id == self.selected_module_id), None)
        if not module:
            detail.update("Selected module not found.")
            return
        payload = module_status(module)
        syntax = Syntax(json.dumps(payload, ensure_ascii=False, indent=2), "json", theme="github-dark", word_wrap=True)
        detail.update(syntax)

    @on(ListView.Highlighted)
    def handle_highlight(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, ModuleItem):
            self.selected_module_id = item.module_id
            self._render_detail()

    def action_refresh(self) -> None:
        self.refresh_dashboard()
        self._write_event("Dashboard refreshed.")

    def action_show_module(self) -> None:
        self._render_detail()

    def action_start_selected(self) -> None:
        self._run_selected_action(start_module, "started")

    def action_stop_selected(self) -> None:
        self._run_selected_action(stop_module, "stopped")

    def action_restart_selected(self) -> None:
        self._run_selected_action(restart_module, "restarted")

    def action_show_logs(self) -> None:
        module_id = self.selected_module_id
        if not module_id:
            self._write_event("[red]No module selected.[/red]")
            return
        path = module_log_path(module_id)
        if not path.exists():
            self._write_event(f"[yellow]No log available for {module_id}.[/yellow]")
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        snippet = "\n".join(lines[-30:]) if lines else "(empty)"
        self._write_event(f"[b]Logs {module_id}[/b]\n{snippet}")

    def action_remove_selected(self) -> None:
        module_id = self.selected_module_id
        if not module_id:
            self._write_event("[red]No module selected.[/red]")
            return
        try:
            stop_module(module_id)
        except Exception:
            pass
        if remove_module(module_id):
            self.selected_module_id = None
            self.refresh_dashboard()
            self._write_event(f"[yellow]Module removed:[/yellow] {module_id}")
        else:
            self._write_event(f"[red]Module could not be removed:[/red] {module_id}")

    def action_configure_llm(self) -> None:
        settings = load_settings()
        self.push_screen(
            DictFormScreen(
                "Configure LLM",
                [
                    {"name": "base_url", "label": "Base URL", "value": settings.llm.base_url},
                    {"name": "model", "label": "Model", "value": settings.llm.model},
                    {"name": "api_key_env", "label": "API key env", "value": settings.llm.api_key_env},
                    {"name": "timeout_seconds", "label": "Timeout seconds", "value": str(settings.llm.timeout_seconds)},
                    {"name": "max_tokens", "label": "Max Tokens", "value": str(settings.llm.max_tokens)},
                ],
            ),
            self._save_llm,
        )

    def action_edit_server(self) -> None:
        settings = load_settings()
        self.push_screen(
            DictFormScreen(
                "Configure Server",
                [
                    {"name": "host", "label": "Host", "value": settings.host},
                    {"name": "port", "label": "Port", "value": str(settings.port)},
                ],
            ),
            self._save_server,
        )

    def action_edit_prompt(self) -> None:
        self.push_screen(
            DictFormScreen(
                "System Prompt",
                [
                    {"name": "prompt", "label": "Prompt", "value": system_prompt()},
                ],
            ),
            self._save_prompt,
        )

    def action_onboard(self) -> None:
        settings = load_settings()
        self.push_screen(
            DictFormScreen(
                "First-run onboarding",
                [
                    {"name": "base_url", "label": "LLM Base URL", "value": settings.llm.base_url or "http://127.0.0.1:8000/v1"},
                    {"name": "model", "label": "Model", "value": settings.llm.model or ""},
                    {"name": "api_key_env", "label": "API key env", "value": settings.llm.api_key_env or "HARBOR_LLM_API_KEY"},
                    {"name": "docs_path", "label": "Docs path (optional)", "value": ""},
                    {"name": "maildir_path", "label": "Maildir path (optional)", "value": ""},
                    {"name": "mcp_base_url", "label": "MCP HTTP URL (optional)", "value": ""},
                ],
                submit_label="Finish",
            ),
            self._save_onboarding,
        )

    def action_add_module(self) -> None:
        self.push_screen(
            DictFormScreen(
                "Create Module",
                [
                    {"name": "type", "label": "Type (docs|maildir|mcp_http)", "value": "docs"},
                    {"name": "id", "label": "Module ID", "value": ""},
                    {"name": "name", "label": "Name", "value": ""},
                    {"name": "path", "label": "Path (local) or empty", "value": ""},
                    {"name": "base_url", "label": "Base URL (remote) or empty", "value": ""},
                    {"name": "port", "label": "Port (local)", "value": str(reserve_port())},
                    {"name": "top_k", "label": "Top K", "value": "5"},
                    {"name": "api_key_env", "label": "API key env", "value": ""},
                ],
                submit_label="Create",
            ),
            self._save_module,
        )

    def action_call_selected(self) -> None:
        module_id = self.selected_module_id
        if not module_id:
            self._write_event("[red]No module selected.[/red]")
            return
        module = next((item for item in load_modules() if item.id == module_id), None)
        default_action = "search" if module and module.type in {"docs", "maildir"} else "health"
        default_payload = '{"query": ""}' if default_action == "search" else "{}"
        self.push_screen(
            DictFormScreen(
                f"Call module: {module_id}",
                [
                    {"name": "action", "label": "Action", "value": default_action},
                    {"name": "payload", "label": "Payload JSON", "value": default_payload},
                ],
                submit_label="Run",
            ),
            self._call_selected_module,
        )

    def action_install_user_service(self) -> None:
        profile_id = self._current_profile_id()
        try:
            result = install_and_optionally_enable_service(profile_id, "user", enable=True)
            self.refresh_dashboard()
            self._write_event(
                f"[green]User service installed for {profile_id}.[/green]\n{json.dumps(result, ensure_ascii=False, indent=2)}"
            )
        except Exception as exc:
            self._write_event(f"[red]Service install failed:[/red] {exc}")

    def action_enable_service(self) -> None:
        profile_id = self._current_profile_id()
        try:
            result = service_action(profile_id, "enable")
            self._write_event(f"[green]Service enabled: {profile_id}[/green]\n{json.dumps(result, ensure_ascii=False, indent=2)}")
        except Exception as exc:
            self._write_event(f"[red]Service enable failed:[/red] {exc}")

    def action_service_status(self) -> None:
        profile_id = self._current_profile_id()
        try:
            result = health_check_service(profile_id)
            self._write_event(f"[b]Service status {profile_id}[/b]\n{json.dumps(result, ensure_ascii=False, indent=2)}")
        except Exception as exc:
            self._write_event(f"[red]Service status failed:[/red] {exc}")

    def action_health_check(self) -> None:
        if self.selected_module_id:
            try:
                result = health_check_module(self.selected_module_id)
                self._write_event(
                    f"[b]Module health {self.selected_module_id}[/b]\n{json.dumps(result, ensure_ascii=False, indent=2)}"
                )
            except Exception as exc:
                self._write_event(f"[red]Module health failed:[/red] {exc}")
            return
        try:
            result = health_check_service("harbor")
            self._write_event(f"[b]Harbor health[/b]\n{json.dumps(result, ensure_ascii=False, indent=2)}")
        except Exception as exc:
            self._write_event(f"[red]Harbor health failed:[/red] {exc}")

    def _run_selected_action(self, action: Any, verb: str) -> None:
        module_id = self.selected_module_id
        if not module_id:
            self._write_event("[red]No module selected.[/red]")
            return
        try:
            result = action(module_id)
            self.refresh_dashboard()
            self._write_event(f"[green]{module_id} {verb}.[/green]\n{json.dumps(result, ensure_ascii=False, indent=2)}")
        except Exception as exc:
            self._write_event(f"[red]Action failed for {module_id}:[/red] {exc}")

    def _save_llm(self, values: dict[str, str] | None) -> None:
        if not values:
            return
        settings = load_settings()
        updated = replace(
            settings,
            llm=replace(
                settings.llm,
                base_url=values["base_url"].strip(),
                model=values["model"].strip(),
                api_key_env=values["api_key_env"].strip(),
                timeout_seconds=float(values["timeout_seconds"] or settings.llm.timeout_seconds),
                max_tokens=int(values["max_tokens"] or settings.llm.max_tokens),
            ),
        )
        save_settings(updated)
        self.refresh_dashboard()
        self._write_event("[green]LLM configuration saved.[/green]")

    def _save_server(self, values: dict[str, str] | None) -> None:
        if not values:
            return
        settings = load_settings()
        save_settings(replace(settings, host=values["host"].strip(), port=int(values["port"] or settings.port)))
        self.refresh_dashboard()
        self._write_event("[green]Server settings saved.[/green]")

    def _save_prompt(self, values: dict[str, str] | None) -> None:
        if not values:
            return
        save_system_prompt(values["prompt"])
        self.refresh_dashboard()
        self._write_event("[green]System prompt saved.[/green]")

    def _save_onboarding(self, values: dict[str, str] | None) -> None:
        if not values:
            return
        settings = load_settings()
        settings.llm.base_url = values["base_url"].strip()
        settings.llm.model = values["model"].strip()
        settings.llm.api_key_env = values["api_key_env"].strip()
        settings.onboarding_complete = True
        save_settings(settings)
        docs_path = values["docs_path"].strip()
        maildir_path = values["maildir_path"].strip()
        mcp_base_url = values["mcp_base_url"].strip()
        if docs_path:
            upsert_module(ModuleConfig(id="docs-local", type="docs", transport="local", path=docs_path, port=reserve_port()))
        if maildir_path:
            upsert_module(ModuleConfig(id="maildir-local", type="maildir", transport="local", path=maildir_path, port=reserve_port()))
        if mcp_base_url:
            upsert_module(ModuleConfig(id="mcp-remote", type="mcp_http", transport="remote", base_url=mcp_base_url))
        self.refresh_dashboard()
        self._write_event("[green]Onboarding completed.[/green]")

    def _save_module(self, values: dict[str, str] | None) -> None:
        if not values:
            return
        module_type = values["type"].strip()
        module_id = values["id"].strip()
        if not module_id:
            self._write_event("[red]Module ID is missing.[/red]")
            return
        if module_type not in {"docs", "maildir", "mcp_http"}:
            self._write_event(f"[red]Unknown module type:[/red] {module_type}")
            return
        if module_type == "mcp_http":
            module = ModuleConfig(
                id=module_id,
                name=values["name"].strip(),
                type="mcp_http",
                transport="remote",
                base_url=values["base_url"].strip(),
                api_key_env=values["api_key_env"].strip(),
                timeout_seconds=30.0,
            )
        else:
            module = ModuleConfig(
                id=module_id,
                name=values["name"].strip(),
                type=parse_module_type(module_type),
                transport="local",
                path=values["path"].strip(),
                port=int(values["port"] or reserve_port()),
                top_k=int(values["top_k"] or 5),
            )
        errors = validate_module_config(module)
        if errors:
            self._write_event("[red]Module invalid:[/red] " + " ".join(errors))
            return
        upsert_module(module)
        self.selected_module_id = module.id
        self.refresh_dashboard()
        self._write_event(f"[green]Module saved:[/green] {module.id}")

    def _call_selected_module(self, values: dict[str, str] | None) -> None:
        module_id = self.selected_module_id
        if not values or not module_id:
            return
        try:
            payload = parse_json_payload(values["payload"])
            result = execute_module(module_id, values["action"].strip(), payload)
            self._write_event(f"[b]Response {module_id}[/b]\n{json.dumps(result, ensure_ascii=False, indent=2)}")
        except Exception as exc:
            self._write_event(f"[red]Module call failed:[/red] {exc}")

    def _write_event(self, message: str) -> None:
        self.query_one("#event-log", RichLog).write(message)

    def _current_profile_id(self) -> str:
        if self.selected_module_id:
            return f"module:{self.selected_module_id}"
        return "harbor"


def _truncate(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def run_tui() -> None:
    HarborTui().run()
