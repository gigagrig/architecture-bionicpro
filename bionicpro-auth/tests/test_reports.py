from test_security import client, isolate, install_session
from app import main as auth
import httpx
import pytest


def test_anonymous_report_is_blocked(client, monkeypatch):
    monkeypatch.setattr(auth, "http", httpx.Client(transport=httpx.MockTransport(
        lambda request: pytest.fail("Anonymous request reached reports API"))))
    assert client.get("/api/reports?from=2026-09-28&to=2026-09-29").status_code == 401
    assert client.get("/reports?from=2026-09-28&to=2026-09-29").status_code == 401


@pytest.mark.parametrize("status", [200, 400, 409, 503])
def test_proxy_uses_server_access_and_rotates_even_on_error(client, monkeypatch, status):
    old, session = install_session(client)
    def backend(request):
        assert request.headers["authorization"] == "Bearer " + session.access
        assert "cookie" not in request.headers
        assert request.url.params.get("from") == "2026-09-28"
        return httpx.Response(status, json={"rows": [], "detail": "test"}, headers={"X-Private":"hidden"})
    monkeypatch.setattr(auth, "http", httpx.Client(transport=httpx.MockTransport(backend)))
    result = client.get("/api/reports?from=2026-09-28&to=2026-09-29")
    assert result.status_code == status
    assert auth.digest(old) not in auth.sessions
    assert "X-Private" not in result.headers
    assert session.access not in result.text
    assert "HttpOnly" in result.headers["set-cookie"]


@pytest.mark.parametrize("status", [204, 401, 403, 503])
def test_cdn_authorization_uses_session_and_private_origin(client, monkeypatch, status):
    old, session = install_session(client)
    uri = "/cdn/reports/v1/test.json?expires=1&signature=test"
    def backend(request):
        assert request.url.path == "/cdn-authorize"
        assert request.headers["authorization"] == "Bearer " + session.access
        assert request.headers["x-original-uri"] == uri
        assert "cookie" not in request.headers
        return httpx.Response(status, headers={"X-Report-Origin":"http://minio:9000/private"})
    monkeypatch.setattr(auth, "http", httpx.Client(transport=httpx.MockTransport(backend)))
    response = client.get("/internal/report-authorize", headers={"X-Original-URI":uri})
    assert response.status_code == status
    assert auth.digest(old) not in auth.sessions
    assert "HttpOnly" in response.headers["set-cookie"]
    assert response.headers.get("X-Report-Origin") == ("http://minio:9000/private" if status == 204 else None)


def test_anonymous_cdn_authorization_never_reaches_origin(client, monkeypatch):
    monkeypatch.setattr(auth, "http", httpx.Client(transport=httpx.MockTransport(
        lambda request: pytest.fail("Anonymous authorization reached Go"))))
    assert client.get("/internal/report-authorize").status_code == 401
