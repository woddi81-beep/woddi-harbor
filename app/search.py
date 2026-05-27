from __future__ import annotations

import email
import json
import logging
import math
import re
import time
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
logger = logging.getLogger(__name__)
PROGRESS_LOG_EVERY = 25
PROGRESS_LOG_INTERVAL_SECONDS = 2.0
INDEX_STALE_CHECK_TTL_SECONDS = 15.0
MAX_PREPARED_SEARCH_CACHE = 8


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


@dataclass
class SearchIndexMeta:
    kind: IndexKind
    root: str
    roots: list[str]
    built_at: str
    document_count: int
    inventory_count: int
    inventory_signature: str


@dataclass
class CachedIndexEntry:
    index: SearchIndex
    file_signature: str
    roots_signature: str
    stale_checked_at: float


@dataclass
class PreparedSearchIndex:
    doc_tokens: list[list[str]]
    document_frequency: dict[str, int]
    cached_at: float


_INDEX_CACHE: dict[str, CachedIndexEntry] = {}
_PREPARED_SEARCH_CACHE: dict[str, PreparedSearchIndex] = {}


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


def _prepare_search_documents(documents: list[IndexedDocument]) -> PreparedSearchIndex:
    doc_tokens = [tokenize(document.title + "\n" + document.text) for document in documents]
    document_frequency: dict[str, int] = {}
    for tokens in doc_tokens:
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    return PreparedSearchIndex(doc_tokens=doc_tokens, document_frequency=document_frequency, cached_at=time.monotonic())


def score_documents(documents: list[IndexedDocument], query: str, top_k: int, prepared: PreparedSearchIndex | None = None) -> list[SearchHit]:
    query_terms = tokenize(query)
    if not query_terms:
        return []
    prepared_index = prepared or _prepare_search_documents(documents)
    doc_tokens = prepared_index.doc_tokens
    document_frequency = prepared_index.document_frequency
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


def _iter_text_paths(root: Path, *, deadline: float | None = None, kind: IndexKind = "docs") -> list[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    for path in root.rglob("*"):
        _check_deadline(deadline, kind, f"dem Dateiscan von {root}")
        if path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS:
            paths.append(path)
    paths.sort()
    return paths


def _iter_mail_paths(root: Path, *, deadline: float | None = None, kind: IndexKind = "maildir") -> list[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    for path in root.rglob("*"):
        _check_deadline(deadline, kind, f"dem Dateiscan von {root}")
        if not path.is_file():
            continue
        if path.suffix.lower() in {".eml", ""} or path.parent.name in {"cur", "new"}:
            paths.append(path)
    paths.sort()
    return paths


def _check_deadline(deadline: float | None, kind: IndexKind, phase: str) -> None:
    if deadline is None:
        return
    if time.monotonic() > deadline:
        raise TimeoutError(f"Indexing fuer {kind} hat das Timeout waehrend {phase} ueberschritten.")


def _deadline_from_timeout(timeout_seconds: float | None) -> float | None:
    if timeout_seconds is None or timeout_seconds <= 0:
        return None
    return time.monotonic() + timeout_seconds


def _log_progress(
    kind: IndexKind,
    root: Path,
    processed: int,
    total: int,
    *,
    phase: str,
    last_logged_at: float,
) -> float:
    now = time.monotonic()
    should_log = processed == total or processed == 1 or processed % PROGRESS_LOG_EVERY == 0
    if not should_log and now - last_logged_at < PROGRESS_LOG_INTERVAL_SECONDS:
        return last_logged_at
    logger.info("Indexing %s: %s %s/%s fuer %s", kind, phase, processed, total, root)
    return now


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


def _inventory_for_roots(
    kind: IndexKind,
    roots: list[tuple[str, str, Path]],
    *,
    timeout_seconds: float | None = None,
    deadline: float | None = None,
) -> tuple[list[tuple[str, str, Path]], list[tuple[str, Path]], list[str], str, int]:
    normalized_roots = [(source_id, source_label, root.expanduser().resolve()) for source_id, source_label, root in roots]
    inventory_items: list[tuple[str, Path]] = []
    root_strings: list[str] = []
    effective_deadline = deadline if deadline is not None else _deadline_from_timeout(timeout_seconds)
    for source_id, _source_label, root in normalized_roots:
        logger.info("Indexing %s: starte Dateiscan fuer %s", kind, root)
        paths = (
            _iter_text_paths(root, deadline=effective_deadline, kind=kind)
            if kind == "docs"
            else _iter_mail_paths(root, deadline=effective_deadline, kind=kind)
        )
        inventory_items.extend((source_id, path) for path in paths)
        root_strings.append(f"{source_id}:{root}")
        logger.info("Indexing %s: Dateiscan fuer %s abgeschlossen, %s Dateien gefunden", kind, root, len(paths))
    signature_parts: list[str] = []
    inventory_count = 0
    for source_id, _source_label, root in normalized_roots:
        _check_deadline(effective_deadline, kind, f"dem Inventarvergleich von {root}")
        source_items = [(item_source, path) for item_source, path in inventory_items if item_source == source_id]
        signature, source_count = _inventory_signature(source_items, root)
        signature_parts.append(signature)
        inventory_count += source_count
    return normalized_roots, inventory_items, root_strings, "\n".join(part for part in signature_parts if part), inventory_count


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


def build_index(
    kind: IndexKind,
    roots: list[tuple[str, str, Path]],
    *,
    timeout_seconds: float | None = None,
) -> SearchIndex:
    deadline = _deadline_from_timeout(timeout_seconds)
    normalized_roots, inventory_items, root_strings, inventory_signature, inventory_count = _inventory_for_roots(
        kind,
        roots,
        deadline=deadline,
    )
    documents: list[IndexedDocument] = []
    for source_id, source_label, root in normalized_roots:
        source_paths = [path for item_source, path in inventory_items if item_source == source_id]
        last_logged_at = 0.0
        total = len(source_paths)
        for index, path in enumerate(source_paths, start=1):
            _check_deadline(deadline, kind, f"dem Lesen von {path}")
            document = (
                _text_to_document(path, source_id=source_id, source_label=source_label)
                if kind == "docs"
                else _mail_to_document(path, source_id=source_id, source_label=source_label)
            )
            if document is not None:
                documents.append(document)
            last_logged_at = _log_progress(
                kind,
                root,
                index,
                total,
                phase="Dateien gelesen",
                last_logged_at=last_logged_at,
            )
    return SearchIndex(
        kind=kind,
        root=root_strings[0] if root_strings else "",
        roots=root_strings,
        built_at=datetime.now(timezone.utc).isoformat(),
        document_count=len(documents),
        inventory_count=inventory_count,
        inventory_signature=inventory_signature,
        documents=documents,
    )


def save_index(index: SearchIndex, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(index)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    meta_path = target.with_suffix(target.suffix + ".meta")
    meta = {
        "kind": index.kind,
        "root": index.root,
        "roots": index.roots,
        "built_at": index.built_at,
        "document_count": index.document_count,
        "inventory_count": index.inventory_count,
        "inventory_signature": index.inventory_signature,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def load_index_meta(path: Path) -> SearchIndexMeta | None:
    meta_path = path.with_suffix(path.suffix + ".meta")
    if meta_path.exists():
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return SearchIndexMeta(
                kind=str(payload.get("kind", "docs")),
                root=str(payload.get("root", "")),
                roots=[str(item) for item in payload.get("roots", [])] or ([str(payload.get("root", ""))] if payload.get("root") else []),
                built_at=str(payload.get("built_at", "")),
                document_count=int(payload.get("document_count", 0)),
                inventory_count=int(payload.get("inventory_count", 0)),
                inventory_signature=str(payload.get("inventory_signature", "")),
            )
    index = load_index(path)
    if index is None:
        return None
    return SearchIndexMeta(
        kind=index.kind,
        root=index.root,
        roots=index.roots,
        built_at=index.built_at,
        document_count=index.document_count,
        inventory_count=index.inventory_count,
        inventory_signature=index.inventory_signature,
    )


def _cache_key_for_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def _roots_signature_for_cache(roots: list[tuple[str, str, Path]]) -> str:
    return "|".join(f"{source_id}:{source_label}:{root.expanduser().resolve()}" for source_id, source_label, root in roots)


def _file_signature(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _prepared_cache_key(index: SearchIndex) -> str:
    return f"{index.kind}|{index.built_at}|{index.document_count}|{index.inventory_count}|{index.root}"


def _prepared_search_index(index: SearchIndex) -> PreparedSearchIndex:
    key = _prepared_cache_key(index)
    cached = _PREPARED_SEARCH_CACHE.get(key)
    if cached is not None:
        cached.cached_at = time.monotonic()
        return cached
    prepared = _prepare_search_documents(index.documents)
    _PREPARED_SEARCH_CACHE[key] = prepared
    if len(_PREPARED_SEARCH_CACHE) > MAX_PREPARED_SEARCH_CACHE:
        oldest_key = min(_PREPARED_SEARCH_CACHE, key=lambda item: _PREPARED_SEARCH_CACHE[item].cached_at)
        _PREPARED_SEARCH_CACHE.pop(oldest_key, None)
    return prepared


def index_is_stale(
    index: SearchIndex,
    roots: list[tuple[str, str, Path]],
    *,
    timeout_seconds: float | None = None,
) -> bool:
    normalized_roots, _inventory_items, root_strings, inventory_signature, inventory_count = _inventory_for_roots(
        index.kind,
        roots,
        timeout_seconds=timeout_seconds,
    )
    normalized_root_strings = [f"{source_id}:{root}" for source_id, _source_label, root in normalized_roots]
    current_roots = index.roots or ([index.root] if index.root else [])
    if normalized_root_strings != current_roots:
        return True
    return (
        inventory_signature != index.inventory_signature
        or inventory_count != index.inventory_count
        or root_strings != current_roots
    )


def ensure_index(
    kind: IndexKind,
    roots: list[tuple[str, str, Path]],
    target: Path,
    *,
    force_rebuild: bool = False,
    timeout_seconds: float | None = None,
) -> tuple[SearchIndex, bool]:
    cache_key = _cache_key_for_path(target)
    roots_signature = _roots_signature_for_cache(roots)
    file_signature = _file_signature(target)
    now = time.monotonic()
    current = None
    if not force_rebuild:
        cached = _INDEX_CACHE.get(cache_key)
        if cached is not None and cached.file_signature == file_signature and cached.roots_signature == roots_signature:
            current = cached.index
            if now - cached.stale_checked_at < INDEX_STALE_CHECK_TTL_SECONDS:
                return current, False
        if current is None:
            current = load_index(target)
        if current is not None and not index_is_stale(current, roots, timeout_seconds=timeout_seconds):
            _INDEX_CACHE[cache_key] = CachedIndexEntry(
                index=current,
                file_signature=file_signature,
                roots_signature=roots_signature,
                stale_checked_at=now,
            )
            return current, False
    rebuilt = build_index(kind, roots, timeout_seconds=timeout_seconds)
    save_index(rebuilt, target)
    _INDEX_CACHE[cache_key] = CachedIndexEntry(
        index=rebuilt,
        file_signature=_file_signature(target),
        roots_signature=roots_signature,
        stale_checked_at=now,
    )
    return rebuilt, True


def search_index(index: SearchIndex, query: str, top_k: int) -> list[SearchHit]:
    return score_documents(index.documents, query, top_k, prepared=_prepared_search_index(index))
