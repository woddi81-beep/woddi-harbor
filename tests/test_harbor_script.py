import os
import subprocess
from pathlib import Path

import pytest

from app.version import __version__

ROOT = Path(__file__).resolve().parents[1]
HARBOR_SCRIPT = ROOT / "harbor.sh"


@pytest.mark.parametrize("argument", ["version", "--version", "-V"])
def test_harbor_script_reports_checked_out_version(argument: str) -> None:
    result = subprocess.run(
        ["bash", str(HARBOR_SCRIPT), argument],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"woddi-harbor {__version__}"


def test_harbor_script_supports_short_version() -> None:
    result = subprocess.run(
        ["bash", str(HARBOR_SCRIPT), "version", "--short"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == __version__


def test_harbor_script_forwards_cli_commands_without_cli_prefix() -> None:
    result = subprocess.run(
        ["bash", str(HARBOR_SCRIPT), "check-prerequisites"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    forwarded = subprocess.run(
        ["bash", str(HARBOR_SCRIPT), "cli", "check-prerequisites"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert forwarded.returncode == 0, forwarded.stderr
    assert result.stdout == forwarded.stdout


def test_harbor_script_installs_missing_cli_before_forwarding(tmp_path: Path) -> None:
    venv_dir = tmp_path / "venv"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    python_stub = bin_dir / "python"
    cli_stub = bin_dir / "woddi-harbor"
    python_stub.write_text(
        """#!/usr/bin/env bash
set -e
if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then
  printf '#!/usr/bin/env bash\\nprintf \"forwarded:%%s\\\\n\" \"$*\"\\n' >"$HARBOR_TEST_CLI"
  chmod +x "$HARBOR_TEST_CLI"
fi
""",
        encoding="utf-8",
    )
    python_stub.chmod(0o755)
    env = os.environ.copy()
    env["HARBOR_VENV_DIR"] = str(venv_dir)
    env["HARBOR_TEST_CLI"] = str(cli_stub)

    result = subprocess.run(
        ["bash", str(HARBOR_SCRIPT), "status"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Erstinstallation automatisch" in result.stdout
    assert "forwarded:status" in result.stdout
