"""Supabase Auth: verify a browser's access token by asking Supabase who it belongs to.

GET {SUPABASE_URL}/auth/v1/user with the project's public apikey and the user's bearer token returns the user, or 401
for an expired/forged token. Results are cached briefly (by token hash) so a burst of API calls costs one lookup.
No service-role key is involved; the server never sees a password."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import httpx

log = logging.getLogger("docuchat.auth")
MAX_CACHE = 2000
NEGATIVE_TTL = 10  # seconds a rejected token stays rejected without asking again


@dataclass(frozen=True)
class User:
    id: str
    email: str | None = None


Fetcher = Callable[[str], "dict | None"]


class TokenVerifier:
    def __init__(self, supabase_url: str, anon_key: str, ttl: int = 60, fetch: Fetcher | None = None,
                 timeout: float = 5.0) -> None:
        self.url = (supabase_url or "").rstrip("/")
        self.anon_key = anon_key or ""
        self.ttl = ttl
        self.timeout = timeout
        self._fetch = fetch or self._fetch_user
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, User | None]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.anon_key)

    def _fetch_user(self, token: str) -> dict | None:
        """Returns the Supabase user JSON, None for a rejected token; raises on network/server errors."""
        r = httpx.get(f"{self.url}/auth/v1/user", timeout=self.timeout,
                      headers={"apikey": self.anon_key, "Authorization": f"Bearer {token}"})
        if r.status_code in (401, 403):
            return None
        r.raise_for_status()
        return r.json()

    def verify(self, token: str | None) -> User | None:
        """The user a token belongs to, or None (no token, bad token, or Supabase unreachable: callers fall back
        to guest identity so the app keeps working)."""
        if not token or not self.enabled or len(token) > 4096 or token.count(".") != 2:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        try:
            data = self._fetch(token)
        except Exception as exc:  # network error or 5xx: don't cache, treat as guest for this request
            log.warning("token verification unavailable: %s", str(exc)[:200])
            return None
        user = User(id=str(data["id"]), email=data.get("email")) if data and data.get("id") else None
        with self._lock:
            if len(self._cache) >= MAX_CACHE:
                for k in [k for k, (exp, _) in self._cache.items() if exp <= now] or list(self._cache)[: MAX_CACHE // 2]:
                    self._cache.pop(k, None)
            self._cache[key] = (now + (self.ttl if user else NEGATIVE_TTL), user)
        return user


def bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() or None if scheme.lower() == "bearer" else None
