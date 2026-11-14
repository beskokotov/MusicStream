"""Small process-local TTL cache; never persists credentials or torrent data."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, TypeVar


T = TypeVar("T")


class TTLCache(Generic[T]):
    def __init__(self, *, ttl_seconds: float, max_entries: int, max_bytes: int):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._items: OrderedDict[object, tuple[float, int, T]] = OrderedDict()
        self._bytes = 0

    def get(self, key: object) -> T | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires, size, value = item
        if time.monotonic() >= expires:
            self._items.pop(key)
            self._bytes -= size
            return None
        self._items.move_to_end(key)
        return value

    def put(self, key: object, value: T, *, size: int = 1,
            ttl_seconds: float | None = None) -> None:
        if size > self.max_bytes:
            return
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= old[1]
        self._items[key] = (time.monotonic() + (ttl_seconds or self.ttl_seconds), size, value)
        self._bytes += size
        while len(self._items) > self.max_entries or self._bytes > self.max_bytes:
            _, (_, removed_size, _) = self._items.popitem(last=False)
            self._bytes -= removed_size
