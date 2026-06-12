#!/usr/bin/env python3
from __future__ import annotations

import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QLockFile, QProcess, QTimer
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / ".venv" / "bin" / "woddi-harbor"
CONFIG = ROOT / "config" / "harbor.json"
ICON_DIR = ROOT / "assets" / "tray"
AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "woddi-harbor-tray.desktop"


def harbor_port() -> int:
    try:
        import json

        payload = json.loads(CONFIG.read_text(encoding="utf-8"))
        return int(payload.get("port", 9680))
    except (OSError, ValueError, TypeError):
        return 9680


def is_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{harbor_port()}/api/health", timeout=0.8) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def autostart_desktop() -> str:
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Name=Woddi Harbor Ampel",
            "Comment=Harbor-Dienste starten und fuer den Spielemodus beenden",
            f"Exec=/usr/bin/python3 {ROOT / 'tools' / 'harbor_tray.py'}",
            f"Icon={ICON_DIR / 'harbor-stopped.svg'}",
            "Terminal=false",
            "X-GNOME-Autostart-enabled=true",
            "X-KDE-autostart-after=panel",
            "",
        ]
    )


class HarborTray:
    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.busy = False
        self.process: QProcess | None = None
        self.tray = QSystemTrayIcon()
        self.menu = QMenu()

        self.status_action = QAction("Status wird geprüft ...")
        self.status_action.setEnabled(False)
        self.start_action = QAction("Harbor starten")
        self.stop_action = QAction("Alles beenden (Spielemodus)")
        self.restart_action = QAction("Harbor neu starten")
        self.open_action = QAction("Harbor im Browser öffnen")
        self.autostart_action = QAction("Beim Anmelden starten")
        self.autostart_action.setCheckable(True)
        self.quit_action = QAction("Ampel beenden")

        self.menu.addAction(self.status_action)
        self.menu.addSeparator()
        self.menu.addAction(self.start_action)
        self.menu.addAction(self.stop_action)
        self.menu.addAction(self.restart_action)
        self.menu.addSeparator()
        self.menu.addAction(self.open_action)
        self.menu.addAction(self.autostart_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.tray.setContextMenu(self.menu)

        self.start_action.triggered.connect(lambda: self.run_runtime_action("start-all", "Harbor wird gestartet ..."))
        self.stop_action.triggered.connect(lambda: self.run_runtime_action("stop-all", "Spielemodus wird aktiviert ..."))
        self.restart_action.triggered.connect(
            lambda: self.run_runtime_action("restart-all", "Harbor wird neu gestartet ...")
        )
        self.open_action.triggered.connect(self.open_browser)
        self.autostart_action.triggered.connect(self.set_autostart)
        self.quit_action.triggered.connect(self.app.quit)
        self.tray.activated.connect(self.activate)

        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(3000)
        self.autostart_action.setChecked(AUTOSTART_FILE.exists())
        self.refresh()
        self.tray.show()

    def set_state(self, state: str, text: str) -> None:
        self.tray.setIcon(QIcon(str(ICON_DIR / f"harbor-{state}.svg")))
        self.tray.setToolTip(f"Woddi Harbor: {text}")
        self.status_action.setText(f"Status: {text}")

    def refresh(self) -> None:
        if self.busy:
            return
        running = is_healthy()
        self.set_state("running" if running else "stopped", "läuft" if running else "gestoppt")
        self.start_action.setEnabled(not running)
        self.stop_action.setEnabled(running)
        self.restart_action.setEnabled(running)
        self.open_action.setEnabled(running)

    def run_runtime_action(self, action: str, message: str) -> None:
        if self.busy:
            return
        if not CLI.exists():
            self.tray.showMessage(
                "Woddi Harbor",
                f"CLI fehlt: {CLI}\nBitte zuerst ./harbor.sh install ausführen.",
                QSystemTrayIcon.MessageIcon.Critical,
            )
            return

        self.busy = True
        self.set_state("starting", message.removesuffix(" ..."))
        self.start_action.setEnabled(False)
        self.stop_action.setEnabled(False)
        self.restart_action.setEnabled(False)
        self.process = QProcess()
        self.process.setWorkingDirectory(str(ROOT))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.finished.connect(lambda code, _status: self.action_finished(action, code))
        self.process.start(str(CLI), ["runtime", action])

    def action_finished(self, action: str, exit_code: int) -> None:
        output = ""
        if self.process is not None:
            output = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        self.busy = False
        self.process = None
        self.refresh()
        if exit_code == 0:
            label = "gestartet" if action == "start-all" else "beendet" if action == "stop-all" else "neu gestartet"
            self.tray.showMessage("Woddi Harbor", f"Harbor wurde {label}.")
        else:
            detail = output[-500:] if output else f"CLI-Endcode {exit_code}"
            self.tray.showMessage("Woddi Harbor", detail, QSystemTrayIcon.MessageIcon.Critical)

    def open_browser(self) -> None:
        webbrowser.open(f"http://127.0.0.1:{harbor_port()}")

    def activate(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick and is_healthy():
            self.open_browser()

    def set_autostart(self, enabled: bool) -> None:
        if enabled:
            AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTOSTART_FILE.write_text(autostart_desktop(), encoding="utf-8")
            AUTOSTART_FILE.chmod(0o755)
        else:
            AUTOSTART_FILE.unlink(missing_ok=True)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Woddi Harbor Ampel")
    app.setQuitOnLastWindowClosed(False)
    lock = QLockFile(str(Path("/tmp") / f"woddi-harbor-tray-{Path.home().name}.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        print("Die Woddi Harbor Ampel läuft bereits.", file=sys.stderr)
        return 0
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("Kein System-Tray verfügbar.", file=sys.stderr)
        return 1
    tray = HarborTray(app)
    app._harbor_tray = tray  # type: ignore[attr-defined]
    app._harbor_tray_lock = lock  # type: ignore[attr-defined]
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
