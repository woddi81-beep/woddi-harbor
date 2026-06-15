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
