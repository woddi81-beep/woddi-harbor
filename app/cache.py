from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar, cast
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
        self._condition = threading.Condition(self._lock)
        self._loading: set[str] = set()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry.stored_at >= self.ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)
            self._expirations += 1

    def _lookup_locked(self, key: str, *, max_age: float, now: float) -> tuple[bool, T | None]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        if now - entry.stored_at >= max_age:
            self._entries.pop(key, None)
            self._expirations += 1
            return False, None
        self._entries.move_to_end(key)
        return True, entry.value

    def get(self, key: str, *, max_age_seconds: float | None = None) -> T | None:
        max_age = self.ttl_seconds if max_age_seconds is None else max(0.0, max_age_seconds)
        now = time.monotonic()
        with self._lock:
            found, value = self._lookup_locked(key, max_age=max_age, now=now)
            if found:
                self._hits += 1
            else:
                self._misses += 1
            return value

    def get_or_load(
        self,
        key: str,
        loader: Callable[[], T],
        *,
        max_age_seconds: float | None = None,
    ) -> T:
        max_age = self.ttl_seconds if max_age_seconds is None else max(0.0, max_age_seconds)
        with self._condition:
            while True:
                found, value = self._lookup_locked(key, max_age=max_age, now=time.monotonic())
                if found:
                    self._hits += 1
                    return cast(T, value)
                if key not in self._loading:
                    self._loading.add(key)
                    self._misses += 1
                    break
                self._condition.wait()

        try:
            value = loader()
        except Exception:
            with self._condition:
                self._loading.discard(key)
                self._condition.notify_all()
            raise

        with self._condition:
            self._set_locked(key, value, time.monotonic())
            self._loading.discard(key)
            self._condition.notify_all()
        return value

    def set(self, key: str, value: T) -> None:
        with self._lock:
            now = time.monotonic()
            self._purge_expired_locked(now)
            self._set_locked(key, value, now)

    def _set_locked(self, key: str, value: T, now: float) -> None:
        self._entries[key] = CacheEntry(value=value, stored_at=now)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self._evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def delete_matching(self, predicate: Callable[[str], bool]) -> int:
        with self._lock:
            keys = [key for key in self._entries if predicate(key)]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    def count_matching(self, predicate: Callable[[str], bool]) -> int:
        with self._lock:
            self._purge_expired_locked(time.monotonic())
            return sum(1 for key in self._entries if predicate(key))

    def stats(self) -> dict[str, int | float]:
        with self._lock:
            self._purge_expired_locked(time.monotonic())
            return {
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "ttl_seconds": self.ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "expirations": self._expirations,
                "loading": len(self._loading),
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
