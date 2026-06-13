from __future__ import annotations

import unittest
from unittest.mock import patch

from app.cache import BoundedTTLCache, SessionRegistry


class CacheTests(unittest.TestCase):
    def test_bounded_ttl_cache_expires_and_evicts_lru_entries(self) -> None:
        with patch("app.cache.time.monotonic", side_effect=[0.0, 0.0, 1.0, 1.0, 2.0, 7.0]):
            cache = BoundedTTLCache[str](ttl_seconds=5.0, max_entries=2)
            cache.set("a", "A")
            cache.set("b", "B")
            self.assertEqual(cache.get("a"), "A")
            cache.set("c", "C")
            self.assertIsNone(cache.get("b"))
            self.assertIsNone(cache.get("a"))

        stats = cache.stats()
        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["evictions"], 1)

    def test_session_registry_is_bounded(self) -> None:
        registry = SessionRegistry(ttl_seconds=60.0, max_entries=2)
        first = registry.create()
        second = registry.create()
        third = registry.create()

        self.assertFalse(registry.contains(first))
        self.assertTrue(registry.contains(second))
        self.assertTrue(registry.contains(third))

    def test_session_registry_refreshes_ttl_on_access(self) -> None:
        with patch("app.cache.time.monotonic", side_effect=[0.0, 4.0, 8.0, 10.0]):
            registry = SessionRegistry(ttl_seconds=5.0, max_entries=2)
            session_id = registry.create()
            self.assertTrue(registry.contains(session_id))
            self.assertTrue(registry.contains(session_id))
            self.assertTrue(registry.contains(session_id))


if __name__ == "__main__":
    unittest.main()
