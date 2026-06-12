from __future__ import annotations

import importlib
import sys
from pathlib import Path

REQUIRED_MODULES = (
    "app",
    "app.backup",
    "app.cli",
    "app.config",
    "app.control",
    "app.runtime",
)


def verify(project_root: Path, *, source_only: bool = False) -> list[str]:
    expected_package = (project_root / "app").resolve()
    errors: list[str] = []

    for module_name in REQUIRED_MODULES:
        relative_path = Path(*module_name.split("."))
        source_path = project_root / f"{relative_path}.py"
        package_path = project_root / relative_path / "__init__.py"
        if not source_path.is_file() and not package_path.is_file():
            errors.append(f"Pflichtmodul fehlt im Checkout: {source_path}")

    if errors:
        return errors
    if source_only:
        return errors

    for module_name in REQUIRED_MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(f"Import fehlgeschlagen: {module_name}: {exc}")
            continue
        module_file = Path(getattr(module, "__file__", "")).resolve()
        if expected_package not in module_file.parents and module_file != expected_package / "__init__.py":
            errors.append(f"Falsches Python-Paket fuer {module_name}: {module_file}")
    return errors


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    source_only = "--source-only" in sys.argv[1:]
    errors = verify(project_root, source_only=source_only)
    if errors:
        print("Woddi-Harbor-Installation ist unvollstaendig oder inkonsistent:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print(
            "Checkout reparieren: git fetch --tags && git checkout v0.3.5",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"Woddi-Harbor-Installation OK: {project_root}")


if __name__ == "__main__":
    main()
