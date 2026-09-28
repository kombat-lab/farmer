from __future__ import annotations

import unittest

from event_cache import BoundedKeyCache


class EventCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_cache_evicts_oldest_key(self) -> None:
        cache = BoundedKeyCache(max_size=2)
        self.assertTrue(cache.remember((1,)))
        self.assertTrue(cache.remember((2,)))
        self.assertFalse(cache.remember((2,)))
        self.assertTrue(cache.remember((3,)))
        self.assertNotIn((1,), cache)
        self.assertIn((2,), cache)
