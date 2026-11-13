"""Bounded, process-local sliding window; no forwarded headers are read."""

import math
import time
from collections import deque
from threading import Lock

from starlette.responses import JSONResponse


class ClientRateLimiter:
    def __init__(self, *, limit=30, window_seconds=60, max_clients=10_000, clock=time.monotonic):
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self.clock = clock
        self._clients: dict[str, deque[float]] = {}
        self._lock = Lock()
        self._next_cleanup = 0.0

    def check(self, client: str) -> int | None:
        """Return Retry-After seconds on rejection, None on acceptance."""
        with self._lock:
            now = self.clock()
            cutoff = now - self.window_seconds
            if now >= self._next_cleanup or (client not in self._clients and len(self._clients) >= self.max_clients):
                self._clients = {key: hits for key, hits in self._clients.items() if hits[-1] > cutoff}
                self._next_cleanup = now + self.window_seconds
            if client not in self._clients:
                if len(self._clients) >= self.max_clients:
                    # Reject instead of evicting active clients and resetting their quota.
                    return self.window_seconds
                self._clients[client] = deque()
            hits = self._clients[client]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(1, math.ceil(hits[0] + self.window_seconds - now))
            hits.append(now)
            return None


class PublicRateLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Uvicorn must run with proxy headers disabled (see README).
            # A future trusted-proxy setup must be deliberate, never trust '*' here.
            peer = scope.get("client")
            client = peer[0] if peer else "unknown"
            retry_after = scope["app"].state.rate_limiter.check(client)
            if retry_after is not None:
                response = JSONResponse(
                    {"detail": "Too many requests; try again later"}, status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
