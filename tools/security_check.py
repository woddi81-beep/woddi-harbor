from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERN = re.compile(r"(api[_-]?key|password|token|secret)[ \t]*[=:][ \t]*['\"]([^'\"\r\n]{8,})", re.IGNORECASE)


def main() -> int:
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {".git", ".venv", "data"} for part in path.parts):
            continue
        if path.suffix.lower() not in {".py", ".json", ".yaml", ".yml", ".toml", ".md", ".js", ".html", ".conf", ".tpl"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in SECRET_PATTERN.finditer(text):
            value = match.group(2)
            if value.startswith(("test-", "__", "${", "<")) or "example" in value.lower():
                continue
            findings.append(f"{path.relative_to(ROOT)}: possible embedded secret for {match.group(1)}")
    print(json.dumps({"ok": not findings, "findings": findings}, indent=2))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
