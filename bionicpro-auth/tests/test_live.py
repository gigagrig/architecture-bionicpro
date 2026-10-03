"""Opt-in HTTPS integration checks against the isolated local Compose project."""
import os
import base64
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
import httpx
import pyotp
import pytest

pytestmark = pytest.mark.skipif(os.getenv("BIONICPRO_E2E") != "1", reason="Set BIONICPRO_E2E=1 for Compose checks")
ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://localhost:3443"
REALM = "/identity/realms/reports-realm"
SID = "__Host-bionicpro-session"


class SecretSettings(dict):
    def __repr__(self):
        return "<local settings: secrets redacted>"


@pytest.fixture(scope="module")
def env():
    return SecretSettings(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if line and not line.startswith("#"))


@pytest.fixture
def browser():
    context = ssl.create_default_context(cafile=str(ROOT / ".local/tls/localhost.crt"))
    with httpx.Client(base_url=ORIGIN, verify=context, follow_redirects=False, timeout=20) as client:
        yield client


@pytest.fixture
def admin(browser, env):
    headers = {}
    expires = 0
    def call(method, path, **kwargs):
        nonlocal expires
        if time.time() >= expires:
            response = browser.post("/identity/realms/master/protocol/openid-connect/token", data={
                "client_id": "admin-cli", "grant_type": "password", "username": "admin",
                "password": env["KEYCLOAK_ADMIN_PASSWORD"]})
            response.raise_for_status()
            headers["Authorization"] = "Bearer " + response.json()["access_token"]
            expires = time.time() + response.json()["expires_in"] - 5
        result = browser.request(method, "/identity/admin/realms/reports-realm" + path, headers=headers, **kwargs)
        result.raise_for_status()
        return result
    return call


@pytest.fixture
def user(admin):
    username = "security-check-" + secrets.token_hex(5)
    password = secrets.token_urlsafe(24)
    response = admin("POST", "/users", json={"username": username, "enabled": True,
        "email": username + "@example.com", "firstName": "Security", "lastName": "Check",
        "requiredActions": ["CONFIGURE_TOTP"], "credentials": [{"type": "password", "value": password, "temporary": False}]})
    identifier = response.headers["location"].rsplit("/", 1)[-1]
    try:
        yield username, password
    finally:
        admin("DELETE", "/users/" + identifier)


def follow_to_form(browser, response):
    for _ in range(12):
        if response.status_code not in (302, 303): return response
        location = urljoin(str(response.url), response.headers["location"])
        if urlsplit(location).path == "/auth/callback": return response
        assert location.startswith(ORIGIN + "/"), "Unexpected external redirect"
        response = browser.get(location)
    pytest.fail("Too many redirects")


def form_submit(browser, response, fields):
    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.find("form", id="kc-totp-settings-form") or soup.find("form", id="kc-otp-login-form") or soup.find("form", id="kc-form-login")
    assert form, response.text[:700]
    data = {item["name"]: item.get("value", "") for item in form.find_all("input", attrs={"name": True}) if item.get("type") == "hidden"}
    data.update(fields)
    return follow_to_form(browser, browser.post(urljoin(str(response.url), form["action"]), data=data))


def login_with_setup(browser, username, password, exchange=True):
    page = follow_to_form(browser, browser.get("/auth/login"))
    page = form_submit(browser, page, {"username": username, "password": password})
    assert not browser.cookies.get(SID), "Password alone must not establish an app session"
    soup = BeautifulSoup(page.text, "html.parser")
    secret = soup.find("input", attrs={"name": "totpSecret"})
    assert secret, "Expected mandatory OTP enrollment: " + page.text[:700]
    # Keycloak's hidden field contains raw secret bytes as text; otpauth uses Base32.
    otp_secret = base64.b32encode(secret["value"].encode()).decode()
    page = form_submit(browser, page, {"totp": pyotp.TOTP(otp_secret).now(), "userLabel": "Integration test"})
    assert page.status_code in (302, 303), page.text[:700]
    callback = urljoin(str(page.url), page.headers["location"])
    assert urlsplit(callback).path == "/auth/callback", callback
    if not exchange:
        return callback
    result = browser.get(callback)
    assert result.headers["location"] == "/", "Backend rejected token exchange"
    assert browser.cookies.get(SID)
    return otp_secret


def test_local_otp_rotation_refresh_and_logout(browser, user):
    login_with_setup(browser, *user)
    original = browser.cookies.get(SID)
    response = browser.get("/auth/session")
    assert response.status_code == 200
    assert response.json()["username"] == user[0]
    assert all(name not in response.text for name in ("access_token", "refresh_token", "id_token"))
    assert all(flag in response.headers["set-cookie"] for flag in ("HttpOnly", "Secure", "SameSite=lax"))
    assert browser.cookies.get(SID) != original
    assert browser.get("/api/protected", headers={"Cookie": f"{SID}={original}"}).status_code == 401
    assert browser.post("/auth/logout").status_code == 403
    # Real access-token expiry; application session must survive.
    time.sleep(122)
    assert browser.get("/api/protected").status_code == 200
    response = browser.get("/auth/session")
    csrf = response.json()["csrf"]
    assert browser.post("/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 200
    assert browser.get("/api/protected").status_code == 401


def test_wrong_otp_is_rejected(browser, user):
    secret = login_with_setup(browser, *user)
    browser.cookies.clear()
    page = follow_to_form(browser, browser.get("/auth/login"))
    page = form_submit(browser, page, {"username": user[0], "password": user[1]})
    assert BeautifulSoup(page.text, "html.parser").find("input", attrs={"name": "otp"})
    valid = {pyotp.TOTP(secret).at(time.time() + offset) for offset in (-30, 0, 30)}
    wrong = next(f"{value:06}" for value in range(10) if f"{value:06}" not in valid)
    page = form_submit(browser, page, {"otp": wrong})
    assert page.status_code == 200
    assert not browser.cookies.get(SID)
    assert BeautifulSoup(page.text, "html.parser").find("input", attrs={"name": "otp"})


def test_pkce_required_and_password_grant_disabled(browser, env):
    response = browser.get(REALM + "/protocol/openid-connect/auth", params={
        "client_id": "bionicpro-auth", "response_type": "code", "scope": "openid",
        "redirect_uri": ORIGIN + "/auth/callback", "state": "test"})
    assert response.status_code == 400 or "error=" in response.headers.get("location", "")
    response = browser.post(REALM + "/protocol/openid-connect/token", data={
        "client_id": "bionicpro-auth", "client_secret": env["AUTH_CLIENT_SECRET"],
        "grant_type": "password", "username": "user1", "password": "password123"})
    assert response.status_code == 400
    assert response.json()["error"] == "unauthorized_client"


def test_wrong_verifier_cannot_exchange_real_code(browser, env, user):
    callback = login_with_setup(browser, *user, exchange=False)
    code = parse_qs(urlsplit(callback).query)["code"][0]
    response = browser.post(REALM + "/protocol/openid-connect/token", data={
        "client_id": "bionicpro-auth", "client_secret": env["AUTH_CLIENT_SECRET"],
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": ORIGIN + "/auth/callback", "code_verifier": "x" * 43})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
    assert not browser.cookies.get(SID)


@pytest.mark.parametrize("username,role", [("john.doe", "prosthetic_user"), ("jane.smith", "user"), ("alex.johnson", "prosthetic_user")])
def test_ldap_password_otp_and_role_mapping(browser, admin, username, role):
    # Synthetic users from ldap/config.ldif; reset only their OTP credentials.
    users = admin("GET", "/users", params={"username": username, "exact": "true"}).json()
    if users:
        identifier = users[0]["id"]
        for credential in admin("GET", f"/users/{identifier}/credentials").json():
            if credential["type"] == "otp": admin("DELETE", f"/users/{identifier}/credentials/{credential['id']}")
    login_with_setup(browser, username, "password")
    session = browser.get("/auth/session")
    assert session.status_code == 200
    assert role in session.json()["roles"]
    users = admin("GET", "/users", params={"username": username, "exact": "true"}).json()
    identifier = users[0]["id"]
    for credential in admin("GET", f"/users/{identifier}/credentials").json():
        if credential["type"] == "otp": admin("DELETE", f"/users/{identifier}/credentials/{credential['id']}")
