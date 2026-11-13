"""Fixed destinations, bounded JSON responses and a shared outbound budget."""

import asyncio
import json
import time
from collections import deque
from urllib.parse import urlsplit

import httpx


class SourceFailure(Exception):
    def __init__(self, reason: str, *, stage=None, diagnostics=None):
        self.reason = reason
        self.stage = stage
        self.diagnostics = diagnostics


class BoundedSourceClient:
    ALLOWED_PATHS = {"/youtube/v3/search", "/youtube/v3/videos"}
    MAX_BYTES = 262144

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._gate = asyncio.Semaphore(2)
        self._requests: deque[float] = deque()

    async def get_json(self, url: str, *, params: dict, api_key: str) -> dict:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise SourceFailure("destination_not_allowed") from exc
        if (parsed.scheme != "https" or parsed.hostname != "www.googleapis.com"
                or parsed.path not in self.ALLOWED_PATHS or port not in (None, 443)
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise SourceFailure("destination_not_allowed")
        async with self._gate:
            now = time.monotonic()
            while self._requests and self._requests[0] <= now - 86400:
                self._requests.popleft()
            if len(self._requests) >= 160 or sum(t > now - 60 for t in self._requests) >= 20:
                raise SourceFailure("request_budget_exhausted")
            self._requests.append(now)
            try:
                async with self.client.stream(
                    "GET", url, params=params, headers={"X-Goog-Api-Key": api_key},
                    timeout=5.0, follow_redirects=False,
                ) as response:
                    if response.status_code == 429 or response.status_code == 403:
                        raise SourceFailure("access_or_quota_denied")
                    if response.status_code != 200:
                        raise SourceFailure("upstream_unavailable")
                    if "application/json" not in response.headers.get("Content-Type", ""):
                        raise SourceFailure("invalid_response")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        body.extend(chunk)
                        if len(body) > self.MAX_BYTES:
                            raise SourceFailure("response_too_large")
                    payload = json.loads(body)
                    if not isinstance(payload, dict) or "error" in payload:
                        raise SourceFailure("invalid_response")
                    return payload
            except httpx.TimeoutException as exc:
                raise SourceFailure("timeout") from exc
            except (httpx.HTTPError, ValueError, UnicodeError) as exc:
                raise SourceFailure("invalid_or_unreachable_response") from exc
