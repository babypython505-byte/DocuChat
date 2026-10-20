"""Per-visitor workspaces and free-tier quotas (in memory; swap for Redis/Postgres when you add accounts)."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date

from .store import VectorStore

IDLE_TTL_SECONDS = 24 * 3600


@dataclass
class Workspace:
    id: str
    store: VectorStore = field(default_factory=VectorStore)
    last_seen: float = field(default_factory=time.time)


class Quota:
    """Counts uses per key per calendar day (UTC)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day = date.today()
        self._counts: dict[str, int] = {}

    def _roll(self) -> None:
        today = date.today()
        if today != self._day:
            self._day, self._counts = today, {}

    def used(self, key: str) -> int:
        with self._lock:
            self._roll()
            return self._counts.get(key, 0)

    def take(self, keys: list[str], limit: int) -> bool:
        """Consume one use for every key if all are under the limit."""
        with self._lock:
            self._roll()
            if any(self._counts.get(k, 0) >= limit for k in keys):
                return False
            for k in keys:
                self._counts[k] = self._counts.get(k, 0) + 1
            return True


class Workspaces:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Workspace] = {}

    def get_or_create(self, ws_id: str | None) -> Workspace:
        with self._lock:
            self._evict()
            ws = self._items.get(ws_id or "")
            if ws is None:
                ws = Workspace(id=secrets.token_urlsafe(16))
                self._items[ws.id] = ws
            ws.last_seen = time.time()
            return ws

    def _evict(self) -> None:
        cutoff = time.time() - IDLE_TTL_SECONDS
        for key in [k for k, w in self._items.items() if w.last_seen < cutoff]:
            del self._items[key]
