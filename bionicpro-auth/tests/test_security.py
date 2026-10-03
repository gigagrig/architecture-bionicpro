import base64
import hashlib
import json
import os
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

os.environ.setdefault("AUTH_CLIENT_SECRET", "unit-test-secret")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("PROFILE_DATABASE_URL", "postgresql://unused")

from app import main as auth

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
PUBLIC_JWK.update(kid="test-key", alg="RS256", use="sig")


def signed(audience, **overrides):
    claims = dict(iss=auth.ISSUER, sub="person-1", aud=audience, azp=auth.CLIENT,
                  iat=int(time.time()), exp=int(time.time()) + 120,
                  realm_access={"roles": ["prosthetic_user"]})
    claims.update(overrides)
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"})


def tokens(nonce="nonce", **overrides):
    return dict(access_token=signed("reports-api", **overrides),
                refresh_token="secret-refresh", id_token=signed(auth.CLIENT, nonce=nonce))


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    auth.sessions.clear()
    auth.pending.clear()
    auth.jwks_cache.update(at=time.time(), keys=[PUBLIC_JWK])
    monkeypatch.setattr(auth, "init_database", lambda: None)
    monkeypatch.setattr(auth, "session_view", lambda s: {"username": s.username, "csrf": s.csrf})
    monkeypatch.setattr(auth, "token_request", lambda fields: tokens())
    monkeypatch.setattr(auth, "http", httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(204))))


@pytest.fixture
def client():
    with TestClient(auth.app, base_url=auth.ORIGIN, follow_redirects=False) as client:
        yield client


def install_session(client, expired=False):
    now = time.time()
    session = auth.Session(signed("reports-api"), auth.CIPHER.encrypt(b"old-refresh"),
                           now - 1 if expired else now + 120, "person-1", "user1", ["user"],
                           None, "csrf-value", now, now)
    sid = auth.random_value()
    auth.sessions[auth.digest(sid)] = session
    client.cookies.set(auth.COOKIE, sid, domain="localhost.local", path="/")
    return sid, session


def test_pkce_and_callback_bind_tokens_to_server_session(client, monkeypatch):
    start = client.get("/auth/login")
    query = parse_qs(urlsplit(start.headers["location"]).query)
    flow = auth.pending[auth.digest(query["state"][0])]
    expected = base64.urlsafe_b64encode(hashlib.sha256(flow["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [expected]
    assert query["code_challenge_method"] == ["S256"]
    assert "verifier" not in start.headers["location"]
    called = []
    def exchange(fields):
        called.append(fields)
        return tokens(query["nonce"][0])
    monkeypatch.setattr(auth, "token_request", exchange)
    result = client.get("/auth/callback", params={"code": "one-use-code", "state": query["state"][0]})
    assert result.headers["location"] == "/"
    assert called[0]["code_verifier"] == flow["verifier"]
    assert len(auth.sessions) == 1
    stored = next(iter(auth.sessions.values()))
    assert b"secret-refresh" not in stored.refresh
    assert auth.CIPHER.decrypt(stored.refresh) == b"secret-refresh"
    assert "secret-refresh" not in str(result.headers)
    assert stored.access not in str(result.headers)
    cookie = result.headers["set-cookie"]
    assert all(flag in cookie for flag in ["Secure", "HttpOnly", "SameSite=lax", "Path=/", "Max-Age=1800"])
    replay = client.get("/auth/callback", params={"code": "one-use-code", "state": query["state"][0]})
    assert replay.headers["location"] == "/?login=failed"
    assert len(called) == 1


@pytest.mark.parametrize("reason", ["state", "binding", "expired", "nonce"])
def test_callback_rejects_invalid_login(client, monkeypatch, reason):
    result = client.get("/auth/login")
    query = parse_qs(urlsplit(result.headers["location"]).query)
    flow = auth.pending[auth.digest(query["state"][0])]
    if reason == "state": query["state"] = ["wrong"]
    if reason == "binding": client.cookies.clear()
    if reason == "expired": flow["created"] -= 301
    # The default token mock has a mismatched nonce.
    response = client.get("/auth/callback", params={"code": "code", "state": query["state"][0]})
    assert response.headers["location"] == "/?login=failed"
    assert not auth.sessions


def test_rotation_rejects_old_id_without_waiting_for_expiry(client, monkeypatch):
    original, session = install_session(client)
    monkeypatch.setattr(auth, "token_request", lambda _: pytest.fail("Unexpected refresh"))
    response = client.get("/api/protected")
    assert response.status_code == 200
    assert auth.digest(original) not in auth.sessions
    assert next(iter(auth.sessions.values())) is session
    fresh = client.cookies.get(auth.COOKIE)
    assert fresh != original
    replay = client.get("/api/protected", headers={"Cookie": f"{auth.COOKIE}={original}"})
    assert replay.status_code == 401
    assert client.get("/api/protected").status_code == 200


def test_refresh_replaces_both_tokens_and_keeps_identity(client, monkeypatch):
    _, session = install_session(client, expired=True)
    calls = []
    def refresh(fields):
        calls.append(fields)
        return tokens()
    monkeypatch.setattr(auth, "token_request", refresh)
    response = client.get("/api/protected")
    assert response.status_code == 200
    assert calls == [{"grant_type": "refresh_token", "refresh_token": "old-refresh"}]
    assert session.expires > time.time()
    assert auth.CIPHER.decrypt(session.refresh) == b"secret-refresh"
    assert session.roles == ["prosthetic_user"]
    assert response.json()["subject"] == "person-1"


@pytest.mark.parametrize("failure", ["invalid_grant", "wrong_subject"])
def test_failed_refresh_clears_session(client, monkeypatch, failure):
    install_session(client, expired=True)
    def fail(fields):
        if failure == "wrong_subject": return tokens(sub="other-person")
        raise httpx.HTTPStatusError("Invalid grant", request=httpx.Request("POST", "https://idp"), response=httpx.Response(400))
    monkeypatch.setattr(auth, "token_request", fail)
    response = client.get("/api/protected")
    assert response.status_code == 401
    assert not auth.sessions
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example", "X-CSRF-Token": "csrf-value"},
                                    {"Origin": auth.ORIGIN, "X-CSRF-Token": "wrong"}])
def test_csrf_protects_mutations(client, headers):
    install_session(client)
    assert client.post("/auth/logout", headers=headers).status_code == 403
    assert len(auth.sessions) == 1


def test_logout_invalidates_session(client):
    sid, _ = install_session(client)
    result = client.post("/auth/logout", headers={"Origin": auth.ORIGIN, "X-CSRF-Token": "csrf-value"})
    assert result.status_code == 200
    assert auth.digest(sid) not in auth.sessions
    assert client.get("/api/protected").status_code == 401


@pytest.mark.parametrize("expired_field,seconds", [("created", auth.ABSOLUTE + 1), ("touched", auth.IDLE + 1)])
def test_session_expiry(client, expired_field, seconds):
    _, session = install_session(client)
    setattr(session, expired_field, time.time() - seconds)
    assert client.get("/api/protected").status_code == 401


@pytest.mark.parametrize("change", [{"iss": "https://evil"}, {"aud": "wrong"}, {"exp": 1}, {"azp": "wrong"}])
def test_jwt_claim_validation(change):
    with pytest.raises((jwt.PyJWTError, ValueError)):
        auth.validate_token(signed("reports-api", **change) if "aud" not in change else signed("wrong"), "reports-api")


def test_bad_signature_rejected():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(dict(iss=auth.ISSUER, aud="reports-api", sub="person-1", azp=auth.CLIENT,
                            iat=int(time.time()), exp=int(time.time()) + 120), other, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(jwt.InvalidSignatureError):
        auth.validate_token(token, "reports-api")


def test_unauthenticated_resource_is_closed(client):
    assert client.get("/api/protected").status_code == 401
    assert client.get("/api/reports").status_code == 401


def test_profile_calls_yandex_only_after_consent(client, monkeypatch):
    _, session = install_session(client)
    session.provider = "yandex"
    calls, writes = [], []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/broker/yandex/token"):
            assert request.headers["authorization"] == "Bearer " + session.access
            return httpx.Response(200, json={"access_token": "external-secret"})
        assert request.url.host == "login.yandex.ru"
        assert request.headers["authorization"] == "OAuth external-secret"
        return httpx.Response(200, json={"id": "123", "login": "person", "default_email": "p@example.com", "birthday": "private"})
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params): writes.append(params)
    monkeypatch.setattr(auth, "http", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(auth, "profile_connection", Connection)
    assert client.get("/auth/session").status_code == 200
    assert not calls and not writes
    assert client.post("/auth/consent").status_code == 403
    assert not calls and not writes
    response = client.post("/auth/consent", headers={"Origin": auth.ORIGIN, "X-CSRF-Token": "csrf-value"})
    assert response.status_code == 200
    assert len(calls) == 2 and len(writes) == 1
    stored = json.loads(auth.CIPHER.decrypt(writes[0][2].encode()))
    assert stored == {"id": "123", "login": "person", "default_email": "p@example.com"}
    assert "external-secret" not in response.text


def test_consent_for_local_account_does_not_contact_yandex(client):
    install_session(client)
    assert client.post("/auth/consent", headers={"Origin": auth.ORIGIN, "X-CSRF-Token": "csrf-value"}).status_code == 400
