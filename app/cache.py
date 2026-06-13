from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar
from uuid import uuid4

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    value: T
    stored_at: float


class BoundedTTLCache(Generic[T]):
    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self.ttl_seconds = max(0.0, ttl_seconds)
        self.max_entries = max(1, max_entries)
        self._entries: OrderedDict[str, CacheEntry[T]] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str, *, max_age_seconds: float | None = None) -> T | None:
        max_age = self.ttl_seconds if max_age_seconds is None else max(0.0, max_age_seconds)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            if now - entry.stored_at >= max_age:
                self._entries.pop(key, None)
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return entry.value

    def set(self, key: str, value: T) -> None:
        with self._lock:
            self._entries[key] = CacheEntry(value=value, stored_at=time.monotonic())
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "ttl_seconds": self.ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
            }


class SessionRegistry:
    def __init__(self, *, ttl_seconds: float = 3600.0, max_entries: int = 1024) -> None:
        self.ttl_seconds = max(1.0, ttl_seconds)
        self.max_entries = max(1, max_entries)
        self._sessions: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def create(self) -> str:
        session_id = str(uuid4())
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            self._sessions[session_id] = now
            while len(self._sessions) > self.max_entries:
                self._sessions.popitem(last=False)
        return session_id

    def contains(self, session_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            created_at = self._sessions.get(session_id)
            if created_at is None:
                return False
            self._sessions[session_id] = now
            self._sessions.move_to_end(session_id)
            return True

    def _purge(self, now: float) -> None:
        while self._sessions:
            session_id, created_at = next(iter(self._sessions.items()))
            if now - created_at < self.ttl_seconds:
                break
            self._sessions.pop(session_id, None)
