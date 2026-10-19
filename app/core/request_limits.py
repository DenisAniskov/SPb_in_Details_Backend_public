"""Bound request memory and anonymous search traffic; no persistent client data."""
import math
import asyncio
import time
from collections import OrderedDict, deque

from starlette.responses import JSONResponse
from app.core.config import settings


class SearchClientLimiter:
    def __init__(self):
        self.clients = OrderedDict()

    def admit(self, client, now):
        window = settings.SEARCH_CLIENT_WINDOW_SECONDS
        while self.clients:
            oldest, attempts = next(iter(self.clients.items()))
            if attempts[-1] > now - window:
                break
            self.clients.pop(oldest)
        attempts = self.clients.get(client)
        if attempts is None:
            # Reject new identities when full; do not evict active limits.
            if len(self.clients) >= settings.SEARCH_CLIENT_MAX_ENTRIES:
                return window
            attempts = deque()
            self.clients[client] = attempts
        while attempts and attempts[0] <= now - window:
            attempts.popleft()
        if len(attempts) >= settings.SEARCH_CLIENT_RATE_LIMIT:
            return max(1, math.ceil(attempts[0] + window - now))
        attempts.append(now)
        self.clients.move_to_end(client)
        return None


class RequestLimitsMiddleware:
    def __init__(self, app):
        self.app = app
        self.limiter = SearchClientLimiter()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def reject(status, detail, retry=None):
            headers = {"Retry-After": str(retry)} if retry else None
            await JSONResponse({"detail": detail}, status_code=status, headers=headers)(scope, receive, send)

        if len(scope.get("query_string", b"")) > settings.QUERY_MAX_BYTES:
            return await reject(414, "Параметры запроса слишком длинные.")
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
            if declared < 0:
                raise ValueError()
        except ValueError:
            return await reject(400, "Некорректный размер запроса.")
        if declared > settings.REQUEST_MAX_BYTES:
            return await reject(413, "Тело запроса слишком большое.")
        if scope["path"].rstrip("/") in {
            settings.API_V1_STR + "/search/suggest",
            settings.API_V1_STR + "/search/geocode",
            settings.API_V1_STR + "/search/reverse-geocode",
        } and scope["method"] == "GET":
            # Uvicorn applies its trusted-proxy policy before ASGI.
            # Never parse client-supplied forwarding headers here.
            peer = scope.get("client")
            retry = self.limiter.admit(peer[0] if peer else "unknown", time.monotonic())
            if retry:
                return await reject(429, "Превышен лимит поисковых запросов. Повторите позже.", retry)
        body = bytearray()
        try:
            async with asyncio.timeout(settings.REQUEST_BODY_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > settings.REQUEST_MAX_BYTES:
                        return await reject(413, "Тело запроса слишком большое.")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "Время получения запроса истекло.")
        replayed = False

        async def limited_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, limited_receive, send)
