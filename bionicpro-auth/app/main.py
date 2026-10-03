"""Same-origin backend for frontend: OAuth tokens never leave the server."""

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt
import psycopg
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

ORIGIN = os.environ.get("PUBLIC_ORIGIN", "https://localhost:3443").rstrip("/")
ISSUER = ORIGIN + "/identity/realms/reports-realm"
INTERNAL = os.environ.get("KEYCLOAK_INTERNAL", "http://keycloak:8080/identity/realms/reports-realm")
CLIENT = "bionicpro-auth"
SECRET = os.environ["AUTH_CLIENT_SECRET"]
CIPHER = Fernet(os.environ["TOKEN_ENCRYPTION_KEY"].encode())
DATABASE = os.environ["PROFILE_DATABASE_URL"]
YANDEX_ENABLED = os.environ.get("YANDEX_ENABLED", "false").lower() == "true"
REPORTS_URL = os.environ.get("REPORTS_URL", "http://reports-api:8080")
COOKIE = "__Host-bionicpro-session"
LOGIN_COOKIE = "__Host-bionicpro-login"
IDLE = 1800
ABSOLUTE = 28800
MAX_SESSIONS = 10000
CONSENT_VERSION = "2026-10-01"
http = httpx.Client(timeout=10, follow_redirects=False)
logger = logging.getLogger("bionicpro.auth")
# BFF and Nginx are trusted proxies on the private local Docker network.
# Production deployments must also encrypt this internal connection.
KC_HEADERS = {"X-Forwarded-Proto": "https"}
lock = threading.RLock()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@dataclass
class Session:
    access: str
    refresh: bytes
    expires: float
    subject: str
    username: str
    roles: list[str]
    provider: str | None
    csrf: str
    created: float
    touched: float


sessions: dict[str, Session] = {}
pending: dict[str, dict] = {}
jwks_cache: dict = {"at": 0, "keys": []}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def random_value() -> str:
    return secrets.token_urlsafe(32)


def cookie(response, name: str, value: str, lifetime: int) -> None:
    response.set_cookie(name, value, max_age=lifetime, secure=True,
                        httponly=True, samesite="lax", path="/")


def clear_cookie(response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")


def prune() -> None:
    now = time.time()
    for key, flow in list(pending.items()):
        if now - flow["created"] >= 300:
            del pending[key]
    for key, session in list(sessions.items()):
        if now - session.touched >= IDLE or now - session.created >= ABSOLUTE:
            del sessions[key]


def validate_token(token: str, audience: str, nonce: str | None = None) -> dict:
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256":
        raise ValueError("Unexpected signature algorithm")
    # Refresh keys once on a cache miss, supporting Keycloak signing-key rotation.
    for attempt in range(2):
        if attempt or time.time() - jwks_cache["at"] > 300:
            result = http.get(INTERNAL + "/protocol/openid-connect/certs", headers=KC_HEADERS)
            result.raise_for_status()
            jwks_cache.update(at=time.time(), keys=result.json()["keys"])
        key = next((k for k in jwks_cache["keys"] if k["kid"] == header.get("kid")), None)
        if key:
            break
    if not key:
        raise ValueError("Unknown signing key")
    claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=["RS256"],
                        issuer=ISSUER, audience=audience,
                        options={"require": ["exp", "iat", "sub", "iss", "aud"]})
    if claims.get("azp") != CLIENT:
        raise ValueError("Unexpected authorized party")
    if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ValueError("Invalid nonce")
    return claims


def token_request(fields: dict) -> dict:
    result = http.post(INTERNAL + "/protocol/openid-connect/token",
                       headers=KC_HEADERS,
                       data={**fields, "client_id": CLIENT, "client_secret": SECRET})
    result.raise_for_status()
    return result.json()


def update_tokens(session: Session, tokens: dict) -> None:
    claims = validate_token(tokens["access_token"], "reports-api")
    if claims["sub"] != session.subject:
        raise ValueError("Subject changed")
    session.access = tokens["access_token"]
    session.refresh = CIPHER.encrypt(tokens["refresh_token"].encode())
    session.expires = claims["exp"]
    session.roles = claims.get("realm_access", {}).get("roles", [])


def profile_connection():
    return psycopg.connect(DATABASE, connect_timeout=5)


def init_database() -> None:
    with profile_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS yandex_profiles (
            subject TEXT PRIMARY KEY, yandex_id TEXT NOT NULL,
            profile_ciphertext TEXT NOT NULL, consent_version TEXT NOT NULL,
            consent_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")


@app.on_event("startup")
def startup() -> None:
    init_database()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/auth/config")
def config():
    return {"yandex_enabled": YANDEX_ENABLED}


@app.get("/auth/login")
def login(request: Request):
    with lock:
        prune()
        if len(pending) >= MAX_SESSIONS:
            return JSONResponse({"detail": "Попробуйте позже"}, status_code=503)
        provider = request.query_params.get("provider")
        if provider not in (None, "yandex") or (provider == "yandex" and not YANDEX_ENABLED):
            return JSONResponse({"detail": "Провайдер недоступен"}, status_code=400)
        state, verifier, nonce, binding = (random_value() for _ in range(4))
        pending[digest(state)] = dict(verifier=verifier, nonce=nonce,
                                      binding=digest(binding), created=time.time())
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = dict(client_id=CLIENT, response_type="code", scope="openid profile email",
                     redirect_uri=ORIGIN + "/auth/callback", state=state, nonce=nonce,
                     code_challenge=challenge, code_challenge_method="S256", prompt="login")
        if provider:
            query["kc_idp_hint"] = provider
        response = RedirectResponse(ISSUER + "/protocol/openid-connect/auth?" + urlencode(query), 302)
        cookie(response, LOGIN_COOKIE, binding, 300)
        return response


@app.get("/auth/callback")
def callback(request: Request):
    with lock:
        prune()
        flow = pending.pop(digest(request.query_params.get("state", "")), None)
        response = RedirectResponse("/?login=failed", 303)
        clear_cookie(response, LOGIN_COOKIE)
        if not flow or not secrets.compare_digest(flow["binding"], digest(request.cookies.get(LOGIN_COOKIE, ""))):
            return response
        if request.query_params.get("error") or not request.query_params.get("code"):
            return response
        if len(sessions) >= MAX_SESSIONS:
            return response
        try:
            tokens = token_request(dict(grant_type="authorization_code", code=request.query_params["code"],
                                        redirect_uri=ORIGIN + "/auth/callback", code_verifier=flow["verifier"]))
            identity = validate_token(tokens["id_token"], CLIENT, flow["nonce"])
            now = time.time()
            session = Session("", b"", 0, identity["sub"], identity.get("preferred_username", ""), [],
                              identity.get("identity_provider"), random_value(), now, now)
            update_tokens(session, tokens)
            # Invalidate any pre-existing application session before issuing a new ID.
            sessions.pop(digest(request.cookies.get(COOKIE, "")), None)
            sid = random_value()
            sessions[digest(sid)] = session
            response = RedirectResponse("/", 303)
            cookie(response, COOKIE, sid, IDLE)
            clear_cookie(response, LOGIN_COOKIE)
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError) as error:
            # Never log authorization codes, token responses or provider profile data.
            logger.warning("Login callback failed: %s; missing_claim=%s",
                           type(error).__name__, getattr(error, "claim", "none"))
        return response


def session_view(session: Session) -> dict:
    with profile_connection() as conn:
        row = conn.execute("SELECT consent_version FROM yandex_profiles WHERE subject=%s",
                           (session.subject,)).fetchone()
    return {"username": session.username, "roles": session.roles, "csrf": session.csrf,
            "provider": session.provider, "profile_saved": bool(row),
            "consent_required": session.provider == "yandex" and (not row or row[0] != CONSENT_VERSION)}


def save_profile(session: Session) -> dict:
    if session.provider != "yandex":
        raise ValueError("Yandex login required")
    # Keycloak releases the external token only to the authenticated backend.
    broker = http.get(INTERNAL + "/broker/yandex/token", headers={**KC_HEADERS, "Authorization": "Bearer " + session.access})
    broker.raise_for_status()
    yandex_token = broker.json()["access_token"]
    result = http.get("https://login.yandex.ru/info?format=json", headers={"Authorization": "OAuth " + yandex_token})
    result.raise_for_status()
    source = result.json()
    profile = {key: source[key] for key in ("id", "login", "first_name", "last_name", "default_email") if key in source}
    ciphertext = CIPHER.encrypt(json.dumps(profile, ensure_ascii=False).encode()).decode()
    with profile_connection() as conn:
        conn.execute("""INSERT INTO yandex_profiles (subject, yandex_id, profile_ciphertext, consent_version)
            VALUES (%s, %s, %s, %s) ON CONFLICT (subject) DO UPDATE SET
            yandex_id=EXCLUDED.yandex_id, profile_ciphertext=EXCLUDED.profile_ciphertext,
            consent_version=EXCLUDED.consent_version, consent_at=now()""",
                     (session.subject, str(source["id"]), ciphertext, CONSENT_VERSION))
    return {"profile_saved": True}


@app.api_route("/auth/{action}", methods=["GET", "POST", "DELETE"])
@app.get("/api/{action}")
@app.get("/reports")
@app.get("/internal/report-authorize")
def protected(request: Request, action: str = "reports"):
    # One worker, one bounded critical section: refresh and rotation are atomic.
    # Stale IDs are rejected, including concurrent requests from another tab.
    with lock:
        prune()
        key = digest(request.cookies.get(COOKIE, ""))
        session = sessions.get(key)
        if session is None:
            return JSONResponse({"detail": "Требуется вход"}, status_code=401)
        if request.method != "GET":
            if (request.headers.get("origin") != ORIGIN or
                    not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf)):
                return JSONResponse({"detail": "CSRF-проверка не пройдена"}, status_code=403)
        try:
            if session.expires <= time.time() + 5:
                update_tokens(session, token_request({"grant_type": "refresh_token",
                              "refresh_token": CIPHER.decrypt(session.refresh).decode()}))
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError):
            sessions.pop(key, None)
            response = JSONResponse({"detail": "Сессия истекла; войдите снова"}, status_code=401)
            clear_cookie(response, COOKIE)
            return response
        try:
            path = request.url.path
            if path == "/auth/session" and request.method == "GET":
                response = JSONResponse(session_view(session))
            elif path == "/auth/consent" and request.method == "POST":
                response = JSONResponse(save_profile(session))
            elif path == "/auth/profile" and request.method == "DELETE":
                with profile_connection() as conn:
                    conn.execute("DELETE FROM yandex_profiles WHERE subject=%s", (session.subject,))
                response = JSONResponse({"profile_saved": False})
            elif path == "/auth/logout" and request.method == "POST":
                sessions.pop(key, None)
                response = JSONResponse({"logged_out": True})
                try:
                    result = http.post(INTERNAL + "/protocol/openid-connect/logout", headers=KC_HEADERS, data={
                        "client_id": CLIENT, "client_secret": SECRET,
                        "refresh_token": CIPHER.decrypt(session.refresh).decode()})
                    result.raise_for_status()
                except httpx.HTTPError:
                    response = JSONResponse({"logged_out": True, "sso_logout_pending": True})
                clear_cookie(response, COOKIE)
                return response
            elif path == "/api/protected":
                response = JSONResponse({"subject": session.subject, "roles": session.roles})
            elif path in ("/api/reports", "/reports"):
                result = http.get(REPORTS_URL + "/reports", params=request.query_params.multi_items(),
                                  headers={"Authorization": "Bearer " + session.access}, timeout=32)
                # Relay only report JSON, never upstream headers or tokens.
                if result.status_code not in (200, 400, 401, 403, 409, 503):
                    raise httpx.HTTPError("Unexpected report service response")
                response = JSONResponse(result.json(), status_code=result.status_code)
            elif path == "/internal/report-authorize":
                result = http.get(REPORTS_URL + "/cdn-authorize", headers={
                    "Authorization": "Bearer " + session.access,
                    "X-Original-URI": request.headers.get("x-original-uri", "")})
                if result.status_code not in (204, 401, 403, 503):
                    raise httpx.HTTPError("Unexpected download authorization response")
                response = Response(status_code=result.status_code)
                if result.status_code == 204:
                    response.headers["X-Report-Origin"] = result.headers["X-Report-Origin"]
            else:
                response = JSONResponse({"detail": "Не найдено"}, status_code=404)
        except (httpx.HTTPError, psycopg.Error):
            response = JSONResponse({"detail": "Внешний сервис временно недоступен"}, status_code=502)
        except (ValueError, KeyError):
            response = JSONResponse({"detail": "Не удалось обработать профиль"}, status_code=400)
        # Rotate even before token expiry. There is no grace alias for the old ID.
        sessions.pop(key)
        sid = random_value()
        session.touched = time.time()
        sessions[digest(sid)] = session
        cookie(response, COOKIE, sid, max(1, min(IDLE, int(session.created + ABSOLUTE - time.time()))))
        return response
