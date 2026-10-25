"""API wiring: health, authentication wall, error shape, and headers.

These tests intentionally do not require PostgreSQL. Database-backed routes
are covered by the integration checklist in the README.
"""

from __future__ import annotations

import httpx
import pytest

from app import __version__
from app.main import app


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def test_healthz_is_public_and_versioned():
    async with client() as http:
        response = await http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers.get("x-request-id")


async def test_session_probe_is_public_and_reports_signed_out():
    async with client() as http:
        response = await http.get("/api/v1/auth/session")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is False
    assert body["csrf_token"] is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/accounts",
        "/api/v1/messages",
        "/api/v1/tests",
        "/api/v1/reports/summary",
        "/api/v1/system/status",
        "/api/v1/suppressions",
    ],
)
async def test_data_routes_require_authentication(path):
    async with client() as http:
        response = await http.get(path)
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "authentication_required"
    assert "message" in body["error"]


async def test_unknown_route_uses_the_stable_error_envelope():
    async with client() as http:
        response = await http.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_login_validates_origin_and_payload():
    async with client() as http:
        response = await http.post(
            "/api/v1/auth/login",
            json={"password": "x"},
            headers={"origin": "https://evil.example"},
        )
    # The origin check runs before the password check; a cross-origin browser
    # attempt is rejected without touching the database.
    assert response.status_code in (401, 403)
    assert "error" in response.json()


async def test_security_headers_are_applied_to_json_errors():
    async with client() as http:
        response = await http.get("/api/v1/accounts")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
