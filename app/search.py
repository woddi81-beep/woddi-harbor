from __future__ import annotations

import email
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email import policy
from pathlib import Path
from typing import Literal


TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]{2,}")
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".rst",
    ".log",
    ".cfg",
    ".conf",
    ".ini",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".csv",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
}

IndexKind = Literal["docs", "maildir"]


@dataclass
class SearchHit:
    score: float
    title: str
    location: str
    snippet: str
    source_id: str = ""
    source_label: str = ""


@dataclass
class IndexedDocument:
    title: str
    location: str
    text: str
    size: int
    mtime_ns: int
    source_id: str = ""
    source_label: str = ""


@dataclass
class SearchIndex:
    kind: IndexKind
    root: str
    roots: list[str]
    built_at: str
    document_count: int
    inventory_count: int
    inventory_signature: str
    documents: list[IndexedDocument]


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


def _snippet(text: str, query_terms: list[str], limit: int = 260) -> str:
    normalized = text.replace("\r", " ").replace("\n", " ")
    if not normalized:
        return ""
    lower = normalized.lower()
    for term in query_terms:
        position = lower.find(term.lower())
        if position >= 0:
            start = max(0, position - 80)
            end = min(len(normalized), position + limit)
            return normalized[start:end].strip()
    return normalized[:limit].strip()


def score_documents(documents: list[IndexedDocument], query: str, top_k: int) -> list[SearchHit]:
    query_terms = tokenize(query)
    if not query_terms:
        return []
    doc_tokens = [tokenize(document.title + "\n" + document.text) for document in documents]
    document_frequency: dict[str, int] = {}
    for tokens in doc_tokens:
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    hits: list[SearchHit] = []
    corpus_size = max(len(doc_tokens), 1)
    for document, tokens in zip(documents, doc_tokens, strict=False):
        if not tokens:
            continue
        token_counts: dict[str, int] = {}
        for token in tokens:
            token_counts[token] = token_counts.get(token, 0) + 1
        score = 0.0
        for term in query_terms:
            tf = token_counts.get(term, 0)
            if tf == 0:
                continue
            idf = math.log(1.0 + corpus_size / (1 + document_frequency.get(term, 0)))
            score += (1.0 + math.log(tf)) * idf
        if score <= 0.0:
            continue
        hits.append(
            SearchHit(
                score=round(score, 4),
                title=document.title,
                location=document.location,
                snippet=_snippet(document.text, query_terms),
                source_id=document.source_id,
                source_label=document.source_label,
            )
        )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:top_k]


def _iter_text_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS:
            paths.append(path)
    return paths


def _iter_mail_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".eml", ""} or path.parent.name in {"cur", "new"}:
            paths.append(path)
    return paths


def _inventory_signature(paths: list[tuple[str, Path]], root: Path) -> tuple[str, int]:
    parts: list[str] = []
    for source_id, path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        parts.append(f"{source_id}|{relative}|{stat.st_size}|{stat.st_mtime_ns}")
    return "\n".join(parts), len(parts)


def _mail_to_document(path: Path, *, source_id: str = "", source_label: str = "") -> IndexedDocument | None:
    try:
        message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    except Exception:
        return None
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                try:
                    parts.append(part.get_content())
                except Exception:
                    continue
    else:
        try:
            parts.append(message.get_content())
        except Exception:
            parts.append("")
    subject = str(message.get("subject", ""))
    sender = str(message.get("from", ""))
    recipients = str(message.get("to", ""))
    content = "\n".join(parts)
    combined = f"Subject: {subject}\nFrom: {sender}\nTo: {recipients}\n\n{content}"
    stat = path.stat()
    return IndexedDocument(
        title=subject or path.name,
        location=str(path),
        text=combined,
        size=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
        source_id=source_id,
        source_label=source_label,
    )


def _text_to_document(path: Path, *, source_id: str = "", source_label: str = "") -> IndexedDocument | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    stat = path.stat()
    return IndexedDocument(
        title=path.name,
        location=str(path),
        text=text,
        size=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
        source_id=source_id,
        source_label=source_label,
    )


def build_index(kind: IndexKind, roots: list[tuple[str, str, Path]]) -> SearchIndex:
    normalized_roots = [(source_id, source_label, root.expanduser().resolve()) for source_id, source_label, root in roots]
    inventory_items: list[tuple[str, Path]] = []
    documents: list[IndexedDocument] = []
    root_strings: list[str] = []
    for source_id, source_label, root in normalized_roots:
        paths = _iter_text_paths(root) if kind == "docs" else _iter_mail_paths(root)
        inventory_items.extend((source_id, path) for path in paths)
        root_strings.append(f"{source_id}:{root}")
        for path in paths:
            document = (
                _text_to_document(path, source_id=source_id, source_label=source_label)
                if kind == "docs"
                else _mail_to_document(path, source_id=source_id, source_label=source_label)
            )
            if document is not None:
                documents.append(document)
    signature_parts: list[str] = []
    inventory_count = 0
    for source_id, source_label, root in normalized_roots:
        source_items = [(item_source, path) for item_source, path in inventory_items if item_source == source_id]
        signature, source_count = _inventory_signature(source_items, root)
        signature_parts.append(signature)
        inventory_count += source_count
    return SearchIndex(
        kind=kind,
        root=root_strings[0] if root_strings else "",
        roots=root_strings,
        built_at=datetime.now(timezone.utc).isoformat(),
        document_count=len(documents),
        inventory_count=inventory_count,
        inventory_signature="\n".join(part for part in signature_parts if part),
        documents=documents,
    )


def save_index(index: SearchIndex, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(index)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_index(path: Path) -> SearchIndex | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    documents = [IndexedDocument(**raw) for raw in payload.get("documents", [])]
    return SearchIndex(
        kind=str(payload.get("kind", "docs")),
        root=str(payload.get("root", "")),
        roots=[str(item) for item in payload.get("roots", [])] or ([str(payload.get("root", ""))] if payload.get("root") else []),
        built_at=str(payload.get("built_at", "")),
        document_count=int(payload.get("document_count", len(documents))),
        inventory_count=int(payload.get("inventory_count", len(documents))),
        inventory_signature=str(payload.get("inventory_signature", "")),
        documents=documents,
    )


def index_is_stale(index: SearchIndex, roots: list[tuple[str, str, Path]]) -> bool:
    normalized_roots = [f"{source_id}:{root.expanduser().resolve()}" for source_id, _source_label, root in roots]
    current_roots = index.roots or ([index.root] if index.root else [])
    if normalized_roots != current_roots:
        return True
    rebuilt = build_index(index.kind, roots)
    return (
        rebuilt.inventory_signature != index.inventory_signature
        or rebuilt.inventory_count != index.inventory_count
        or rebuilt.document_count != index.document_count
    )


def ensure_index(
    kind: IndexKind,
    roots: list[tuple[str, str, Path]],
    target: Path,
    *,
    force_rebuild: bool = False,
) -> tuple[SearchIndex, bool]:
    current = None if force_rebuild else load_index(target)
    if current is not None and not index_is_stale(current, roots):
        return current, False
    rebuilt = build_index(kind, roots)
    save_index(rebuilt, target)
    return rebuilt, True


def search_index(index: SearchIndex, query: str, top_k: int) -> list[SearchHit]:
    return score_documents(index.documents, query, top_k)
