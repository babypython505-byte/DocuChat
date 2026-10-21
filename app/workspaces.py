"""Per-visitor workspaces and free-tier quotas (in memory; swap for Redis/Postgres when you add accounts).

Render's free tier wipes memory on every restart, so a workspace is cheap to rebuild: the browser keeps each
document's chunks and vectors in IndexedDB and re-hydrates the workspace via POST /api/documents/restore."""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date

from .store import VectorStore

IDLE_TTL_SECONDS = 24 * 3600
MAX_WORKSPACES = 400  # least-recently-used workspaces are dropped beyond this (keeps RAM bounded)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


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

    def take(self, keys: list[str], limit: int, n: int = 1) -> bool:
        """Consume n uses for every key if all stay within the limit."""
        with self._lock:
            self._roll()
            if any(self._counts.get(k, 0) + n > limit for k in keys):
                return False
            for k in keys:
                self._counts[k] = self._counts.get(k, 0) + n
            return True

    def give_back(self, keys: list[str], n: int = 1) -> None:
        """Refund uses when the AI call failed, so users aren't charged for our errors."""
        with self._lock:
            self._roll()
            for k in keys:
                self._counts[k] = max(0, self._counts.get(k, 0) - n)


class Workspaces:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[str, Workspace] = OrderedDict()

    def get_or_create(self, ws_id: str | None) -> Workspace:
        """Known id -> that workspace. Unknown but well-formed id (e.g. after a server restart) -> a fresh, empty
        workspace under the same id, so the browser's cookie stays valid and it can restore its documents."""
        with self._lock:
            self._evict()
            ws = self._items.get(ws_id or "")
            if ws is None:
                new_id = ws_id if ws_id and _ID_RE.match(ws_id) else secrets.token_urlsafe(16)
                ws = Workspace(id=new_id)
                self._items[ws.id] = ws
            self._items.move_to_end(ws.id)
            ws.last_seen = time.time()
            while len(self._items) > MAX_WORKSPACES:
                self._items.popitem(last=False)
            return ws

    def clear(self) -> None:
        """Drop every workspace (what a server restart does on Render's free tier)."""
        with self._lock:
            self._items.clear()

    def _evict(self) -> None:
        cutoff = time.time() - IDLE_TTL_SECONDS
        for key in [k for k, w in self._items.items() if w.last_seen < cutoff]:
            del self._items[key]
