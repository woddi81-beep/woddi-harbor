from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
HTTP_ROOT = ROOT.parent
OPERATIONS_TARGET = ROOT / "data/sources/documentation-operation"
CUSTOMER_TARGET = ROOT / "data/sources/documentation-customer"


@dataclass(frozen=True)
class SourceFile:
    source: Path
    target_name: str
    category: str
    convert_html: bool = False


OPERATIONS_FILES = [
    SourceFile(ROOT / "README.md", "README.md", "overview"),
    SourceFile(ROOT / "CHANGELOG.md", "CHANGELOG.md", "release"),
    SourceFile(ROOT / "docs/ARCHITECTURE.md", "ARCHITECTURE.md", "architecture"),
    SourceFile(ROOT / "docs/HOWTO.md", "HOWTO.md", "operations"),
    SourceFile(ROOT / "docs/INSTALL.md", "INSTALL.md", "operations"),
    SourceFile(ROOT / "docs/OPERATIONS.md", "OPERATIONS.md", "operations"),
    SourceFile(ROOT / "docs/PRODUCT.md", "PRODUCT.md", "product"),
    SourceFile(ROOT / "docs/PRIVACY.md", "PRIVACY.md", "governance"),
    SourceFile(ROOT / "docs/RUNBOOK.md", "RUNBOOK.md", "operations"),
    SourceFile(ROOT / "docs/SECURITY.md", "SECURITY.md", "security"),
    SourceFile(ROOT / "docs/SLO.md", "SLO.md", "operations"),
    SourceFile(ROOT / "docs/UPGRADE.md", "UPGRADE.md", "operations"),
]

CUSTOMER_FILES = [
    SourceFile(
        HTTP_ROOT / "asv-landing/dokumentation/index.html",
        "ASV_ENDUSER_GUIDE.md",
        "end-user",
        convert_html=True,
    ),
    SourceFile(HTTP_ROOT / "asv-platform/README.md", "ASV_PLATFORM_OVERVIEW.md", "product"),
    SourceFile(HTTP_ROOT / "asv-platform/changelog.md", "ASV_CHANGELOG.md", "release"),
    SourceFile(HTTP_ROOT / "asv-platform/RELEASE_ROLLOUT.md", "ASV_RELEASE_ROLLOUT.md", "operations"),
    SourceFile(
        HTTP_ROOT / "asv-platform/MODERNISIERUNGSPLAN_APP.md",
        "ASV_MODERNIZATION_PLAN.md",
        "architecture",
    ),
    SourceFile(
        HTTP_ROOT / "asv-user-mobile-app/docs/ARCHITEKTUR.md",
        "ASV_MOBILE_ARCHITECTURE.md",
        "architecture",
    ),
    SourceFile(
        HTTP_ROOT / "asv-user-mobile-app/docs/COMPLIANCE_DSGVO.md",
        "ASV_MOBILE_PRIVACY.md",
        "governance",
    ),
]


def _html_to_markdown(path: Path) -> str:
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    for element in soup(["script", "style", "nav", "svg"]):
        element.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else path.stem
    lines = [f"# {title}", ""]
    for element in soup.find_all(["h1", "h2", "h3", "p", "li", "th", "td"]):
        text = " ".join(element.get_text(" ", strip=True).split())
        if not text:
            continue
        if element.name == "h1":
            lines.extend([f"# {text}", ""])
        elif element.name == "h2":
            lines.extend([f"## {text}", ""])
        elif element.name == "h3":
            lines.extend([f"### {text}", ""])
        elif element.name == "li":
            lines.append(f"- {text}")
        else:
            lines.extend([text, ""])
    return "\n".join(lines).strip() + "\n"


def _write_corpus(target: Path, files: list[SourceFile], *, corpus: str) -> dict[str, object]:
    target.mkdir(parents=True, exist_ok=True)
    for existing in target.iterdir():
        if existing.is_file():
            existing.unlink()
        elif existing.is_dir():
            shutil.rmtree(existing)

    manifest_files: list[dict[str, object]] = []
    total_bytes = 0
    for item in files:
        if not item.source.is_file():
            raise FileNotFoundError(f"Required source document is missing: {item.source}")
        content = _html_to_markdown(item.source) if item.convert_html else item.source.read_text(encoding="utf-8")
        destination = target / item.target_name
        destination.write_text(content, encoding="utf-8")
        encoded = content.encode("utf-8")
        total_bytes += len(encoded)
        manifest_files.append(
            {
                **asdict(item),
                "source": str(item.source),
                "target": str(destination),
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )

    manifest: dict[str, object] = {
        "corpus": corpus,
        "generated_at": time.time(),
        "files": manifest_files,
        "file_count": len(manifest_files),
        "bytes": total_bytes,
        "policy": "Explicit allowlist; no uploads, databases, credentials, runtime state, or private user data.",
    }
    (target / "_SOURCE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build curated Harbor reference corpora.")
    parser.add_argument("--operations-only", action="store_true")
    parser.add_argument("--customer-only", action="store_true")
    args = parser.parse_args()
    if args.operations_only and args.customer_only:
        parser.error("Choose at most one corpus filter.")

    result: dict[str, object] = {}
    if not args.customer_only:
        result["operations"] = _write_corpus(OPERATIONS_TARGET, OPERATIONS_FILES, corpus="harbor-operations")
    if not args.operations_only:
        result["customer"] = _write_corpus(CUSTOMER_TARGET, CUSTOMER_FILES, corpus="asv-public-documentation")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
