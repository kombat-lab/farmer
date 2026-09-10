from __future__ import annotations

from collections import deque
from collections.abc import Hashable
from typing import Generic, TypeVar

KeyT = TypeVar("KeyT", bound=Hashable)


class BoundedKeyCache(Generic[KeyT]):
    """Хранит ограниченное число ключей и отбрасывает самый старый."""

    def __init__(self, max_size: int = 500) -> None:
        if max_size <= 0:
            raise ValueError("Cache capacity must be positive")
        self._keys: set[KeyT] = set()
        self._order: deque[KeyT] = deque(maxlen=max_size)

    def remember(self, key: KeyT) -> bool:
        if key in self._keys:
            return False

        if len(self._order) == self._order.maxlen:
            oldest = self._order.popleft()
            self._keys.discard(oldest)

        self._order.append(key)
        self._keys.add(key)
        return True

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    def discard(self, key: KeyT) -> None:
        """Forgets a key when an action was rejected before reaching the game."""
        if key not in self._keys:
            return
        self._keys.discard(key)
        try:
            self._order.remove(key)
        except ValueError:
            pass
