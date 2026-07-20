from __future__ import annotations

from collections import deque


class ProcessedEventCache:
    """Ограниченный кэш для защиты от повторной обработки Telegram-событий."""

    def __init__(self, max_size: int = 500) -> None:
        self._keys: set[tuple] = set()
        self._order: deque[tuple] = deque(maxlen=max_size)

    def remember(self, message) -> bool:
        edit_timestamp = (
            message.edit_date.timestamp()
            if message.edit_date
            else None
        )
        key = (
            message.id,
            edit_timestamp,
            message.raw_text or "",
        )

        if key in self._keys:
            return False

        if len(self._order) == self._order.maxlen:
            oldest = self._order.popleft()
            self._keys.discard(oldest)

        self._order.append(key)
        self._keys.add(key)
        return True
