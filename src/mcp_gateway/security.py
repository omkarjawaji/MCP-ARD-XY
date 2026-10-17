"""Inbound reverse-proxy layer: bearer auth, rate limiting, per-client tool allow-lists.

HTTP requests pass through `AuthRateLimitMiddleware` (an ASGI middleware) which
authenticates the caller and stores their `Client` in a ContextVar. The FastMCP
middleware in `gateway.py` reads that ContextVar to filter and authorise tools.
Over stdio there is no network boundary, so the local client gets full access.
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class Client:
    id: str
    api_key: str = field(repr=False, default="")
    allow_tools: tuple[str, ...] = ("*",)
    rate_limit_per_min: int = 60

    def allows(self, tool: str) -> bool:
        return any(fnmatchcase(tool, pat) for pat in self.allow_tools)


LOCAL_CLIENT = Client(id="stdio-local")
current_client: ContextVar[Client] = ContextVar("current_client", default=LOCAL_CLIENT)


def load_clients(path: Path) -> list[Client]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))  # tolerate a BOM from Windows editors
    return [
        Client(
            id=c["id"],
            api_key=c["api_key"],
            allow_tools=tuple(c.get("allow_tools", ["*"])),
            rate_limit_per_min=int(c.get("rate_limit_per_min", 60)),
        )
        for c in data["clients"]
    ]


def write_example_clients(path: Path) -> None:
    """Create a clients file with freshly generated keys (never overwrites)."""
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    clients = [
        {"id": "admin", "api_key": secrets.token_urlsafe(32), "allow_tools": ["*"], "rate_limit_per_min": 120},
        {"id": "decider-eval", "api_key": secrets.token_urlsafe(32),
         "allow_tools": ["search_tools", "describe_tool", "call_tool", "genie_*", "docs_*"],
         "rate_limit_per_min": 60},
        {"id": "readonly-web", "api_key": secrets.token_urlsafe(32),
         "allow_tools": ["fetch_*", "time_*", "fx_*"], "rate_limit_per_min": 30},
    ]
    path.write_text(json.dumps({"clients": clients}, indent=2), encoding="utf-8")


class SlidingWindow:
    """Per-key sliding-window limiter. Returns seconds to wait, or 0 if allowed."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window: float = 60.0) -> float:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= limit:
            return window - (now - q[0])
        q.append(now)
        return 0.0


class AuthRateLimitMiddleware:
    def __init__(self, app: ASGIApp, clients: list[Client]):
        self.app = app
        self.clients = clients
        self.limiter = SlidingWindow()

    def _authenticate(self, scope: Scope) -> Client | None:
        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        found: Client | None = None
        for c in self.clients:  # compare against every key to avoid early-exit timing leaks
            if hmac.compare_digest(token.encode(), c.api_key.encode()):
                found = c
        return found

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = self._authenticate(scope)
        if client is None:
            resp = JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
            await resp(scope, receive, send)
            return
        wait = self.limiter.check(client.id, client.rate_limit_per_min)
        if wait > 0:
            resp = JSONResponse({"error": "rate limit exceeded", "client": client.id}, status_code=429,
                                headers={"Retry-After": str(int(wait) + 1)})
            await resp(scope, receive, send)
            return
        token = current_client.set(client)
        try:
            await self.app(scope, receive, send)
        finally:
            current_client.reset(token)
