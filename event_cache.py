from __future__ import annotations

from collections import deque


class BoundedKeyCache:
    """Хранит ограниченное число ключей и отбрасывает самый старый."""

    def __init__(self, max_size: int = 500) -> None:
        self._keys: set[tuple] = set()
        self._order: deque[tuple] = deque(maxlen=max_size)

    def remember(self, key: tuple) -> bool:
        if key in self._keys:
            return False

        if len(self._order) == self._order.maxlen:
            oldest = self._order.popleft()
            self._keys.discard(oldest)

        self._order.append(key)
        self._keys.add(key)
        return True

    def __contains__(self, key: tuple) -> bool:
        return key in self._keys

    def discard(self, key: tuple) -> None:
        """Forgets a key when an action was rejected before reaching the game."""
        if key not in self._keys:
            return
        self._keys.discard(key)
        try:
            self._order.remove(key)
        except ValueError:
            pass
