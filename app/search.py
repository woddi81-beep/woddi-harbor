from __future__ import annotations

import email
import math
import re
from dataclasses import dataclass
from email import policy
from pathlib import Path


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


@dataclass
class SearchHit:
    score: float
    title: str
    location: str
    snippet: str


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


def score_documents(documents: list[tuple[str, str, str]], query: str, top_k: int) -> list[SearchHit]:
    query_terms = tokenize(query)
    if not query_terms:
        return []
    doc_tokens = [tokenize(title + "\n" + text) for title, _location, text in documents]
    document_frequency: dict[str, int] = {}
    for tokens in doc_tokens:
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    hits: list[SearchHit] = []
    corpus_size = max(len(doc_tokens), 1)
    for (title, location, text), tokens in zip(documents, doc_tokens, strict=False):
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
                title=title,
                location=location,
                snippet=_snippet(text, query_terms),
            )
        )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:top_k]


def collect_text_documents(root: Path) -> list[tuple[str, str, str]]:
    if not root.exists():
        return []
    documents: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        documents.append((path.name, str(path), text))
    return documents


def collect_mail_documents(root: Path) -> list[tuple[str, str, str]]:
    if not root.exists():
        return []
    documents: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".eml", ""} and path.parent.name not in {"cur", "new"}:
            continue
        try:
            message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        except Exception:
            continue
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
        title = subject or path.name
        documents.append((title, str(path), combined))
    return documents
