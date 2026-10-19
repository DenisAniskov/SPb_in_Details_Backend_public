import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.config import settings
from app.core.request_limits import RequestLimitsMiddleware, SearchClientLimiter


def limited_app():
    app = FastAPI()
    app.add_middleware(RequestLimitsMiddleware)
    app.get("/api/v1/search/suggest")(lambda: {"results": []})
    app.post("/echo")(lambda: {"ok": True})
    return app


@pytest.mark.parametrize("trusted,peer,expected", [
    ("", "127.0.0.1", [200, 200, 429]),
    ("127.0.0.1", "198.51.100.1", [200, 200, 429]),
    ("127.0.0.1", "127.0.0.1", [200, 200, 200]),
])
async def test_forwarded_headers_only_used_from_explicit_proxy(monkeypatch, trusted, peer, expected):
    monkeypatch.setattr(settings, "SEARCH_CLIENT_RATE_LIMIT", 2)
    app = ProxyHeadersMiddleware(limited_app(), trusted_hosts=trusted)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1234)),
                                base_url="http://test") as client:
        responses = [await client.get("/api/v1/search/suggest", headers={
            "X-Forwarded-For": f"203.0.113.{i}", "X-Real-IP": f"203.0.113.{i}"
        }) for i in range(3)]
    assert [r.status_code for r in responses] == expected
    if expected[-1] == 429:
        assert int(responses[-1].headers["Retry-After"]) > 0


def test_client_state_is_bounded_fail_closed_and_expires(monkeypatch):
    monkeypatch.setattr(settings, "SEARCH_CLIENT_RATE_LIMIT", 1)
    monkeypatch.setattr(settings, "SEARCH_CLIENT_MAX_ENTRIES", 1)
    limiter = SearchClientLimiter()
    assert limiter.admit("first", 0) is None
    assert limiter.admit("second", 1) == 60
    assert limiter.admit("first", 1) == 59
    assert len(limiter.clients) == 1
    assert limiter.admit("second", 61) is None
    assert list(limiter.clients) == ["second"]


async def test_request_size_checks_query_declared_and_streamed(monkeypatch):
    app = limited_app()
    monkeypatch.setattr(settings, "REQUEST_MAX_BYTES", 1024)
    monkeypatch.setattr(settings, "QUERY_MAX_BYTES", 256)
    async def chunks():
        yield b"x" * 700
        yield b"x" * 700
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/echo", params={"q": "x" * 300})).status_code == 414
        assert (await client.post("/echo", content=b"x" * 1025)).status_code == 413
        assert (await client.post("/echo", content=chunks())).status_code == 413
        assert (await client.post("/echo", headers={"Content-Length": "bad"})).status_code == 400
        assert (await client.post("/echo", headers={"Content-Length": "-1"})).status_code == 400
        assert (await client.post("/echo", content=b"{}")).status_code == 200


async def test_validation_does_not_echo_body_query_or_coordinates(client):
    secret = "private-synthetic-value"
    response = await client.post("/api/v1/routes/propose-walk", json={
        "start_point": {"latitude": 59.9876543, "longitude": secret}})
    assert response.status_code == 422
    assert secret not in response.text and "59.9876543" not in response.text
    assert all(set(error) == {"type", "loc", "msg"} for error in response.json()["detail"])
    response = await client.get("/api/v1/search/suggest", params={"text": secret * 20})
    assert response.status_code == 422 and secret not in response.text


async def test_database_error_message_and_logs_are_safe(client, monkeypatch, caplog):
    from app.services.place_service import PlaceService
    error = OperationalError("secret SQL gps 59.9876543", {"key": "private-key"}, Exception("private-path"))
    monkeypatch.setattr(PlaceService, "get_places", AsyncMock(side_effect=error))
    with caplog.at_level(logging.ERROR):
        response = await client.get("/api/v1/places")
    assert response.status_code == 503
    for sensitive in ["private-key", "private-path", "59.9876543", "secret SQL"]:
        assert sensitive not in response.text and sensitive not in caplog.text


async def test_general_error_response_safe(monkeypatch, caplog):
    import app.main as main
    response = await main.unexpected_exception_handler(None, RuntimeError("secret-gps-private"))
    assert response.status_code == 500
    assert b"secret-gps-private" not in response.body
    assert "secret-gps-private" not in caplog.text


async def test_default_cors_disallows_browser_origin(client):
    response = await client.options("/api/v1/places", headers={
        "Origin": "https://untrusted.example", "Access-Control-Request-Method": "GET"})
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_server_traceback_logs_do_not_disclose_inputs(caplog):
    from app.core.logging_safety import SafeServerErrors
    logger = logging.getLogger("test.safe.server")
    guard = SafeServerErrors()
    logger.addFilter(guard)
    try:
        with caplog.at_level(logging.ERROR):
            try:
                raise RuntimeError("secret-coordinate-credential")
            except RuntimeError:
                logger.exception("ASGI failure")
            logger.error("Traceback (most recent call last):\nprivate-path secret-coordinate-credential")
        assert "secret-coordinate-credential" not in caplog.text
        assert "private-path" not in caplog.text
        assert "RuntimeError" in caplog.text
    finally:
        logger.removeFilter(guard)


async def test_slow_body_times_out_before_endpoint_runs(monkeypatch):
    import asyncio
    monkeypatch.setattr(settings, "REQUEST_BODY_TIMEOUT_SECONDS", 0.01)
    events = []
    called = False
    async def endpoint(scope, receive, send):
        nonlocal called
        called = True
    async def stalled():
        await asyncio.Event().wait()
    async def capture(message):
        events.append(message)
    middleware = RequestLimitsMiddleware(endpoint)
    await middleware({"type": "http", "path": "/health", "method": "GET",
                      "query_string": b"", "headers": [(b"content-length", b"1")]},
                     stalled, capture)
    assert events[0]["status"] == 408
    assert not called
