"""A tiny thread-safe TTL cache for metadata lookups.

Extraction is the expensive part of every request (a handful of HTTPS round
trips to the platform). Caching it for a few minutes makes the common
"resolve, show the user, then download" flow cost one extraction instead of
two, and blunts repeated hits on the same link.

Scope is per process. With more than one instance or worker, each keeps its own
copy, which is fine for a read-only cache of public metadata.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    def __init__(self, ttl_sec: int, max_entries: int) -> None:
        self._ttl = max(0, ttl_sec)
        self._max = max(1, max_entries)
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, value = entry
            if expires_at <= now:
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        expires_at = time.monotonic() + self._ttl
        with self._lock:
            self._data[key] = (expires_at, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def purge_expired(self) -> int:
        if not self.enabled:
            return 0
        now = time.monotonic()
        with self._lock:
            stale = [k for k, (expires_at, _) in self._data.items() if expires_at <= now]
            for key in stale:
                del self._data[key]
        return len(stale)

    def stats(self) -> dict:
        with self._lock:
            size = len(self._data)
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "entries": size,
            "max_entries": self._max,
            "ttl_sec": self._ttl,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else None,
        }
