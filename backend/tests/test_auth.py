"""Sign-in: password hash, sessions, lockout, one-time codes, and the network listener's rules."""
import pytest
from fastapi.testclient import TestClient

from app import auth, network
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def over_network(monkeypatch):
    monkeypatch.setattr(network, "is_network_request", lambda scope: True)


def test_password_rules_and_hashing():
    with pytest.raises(auth.AuthError):
        auth.set_password("short")
    auth.set_password("a long enough password")
    assert auth.has_password() and "a long enough password" not in auth._config("password_hash")
    assert auth._check_password("a long enough password") and not auth._check_password("a long enough passwore")


def test_login_session_cookie_flags_and_logout(client, over_network):
    auth.set_password("correct horse battery")
    assert client.get("/api/sheets").status_code == 401  # no session
    assert client.get("/api/auth/status").json() == {"network": True, "password_set": True, "signed_in": False}
    assert client.post("/api/auth/login", json={"password": "nope nope nope"}).status_code == 401
    r = client.post("/api/auth/login", json={"password": "correct horse battery"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/sheets").status_code == 200  # the cookie is now sent
    assert client.post("/api/sheets", json={"name": "x"}, headers={"origin": "http://evil.example"}).status_code == 403
    assert len(auth.sessions()) >= 1
    client.post("/api/auth/logout")
    assert client.get("/api/sheets").status_code == 401


def test_lockout_after_repeated_wrong_passwords(client, over_network):
    auth.set_password("correct horse battery")
    auth._fails.clear()
    for _ in range(auth.MAX_FAILS):
        assert client.post("/api/auth/login", json={"password": "wrong wrong wrong"}).status_code == 401
    locked = client.post("/api/auth/login", json={"password": "correct horse battery"})
    assert locked.status_code == 401 and "Too many" in locked.json()["detail"]
    auth._fails.clear()


def test_management_is_desktop_only_and_codes_are_single_use(client, monkeypatch):
    code = client.post("/api/auth/open-code").json()["code"]  # allowed: not over the network
    assert client.post("/api/auth/exchange", json={"code": code}).status_code == 200
    assert client.post("/api/auth/exchange", json={"code": code}).status_code == 401  # used once
    monkeypatch.setattr(network, "is_network_request", lambda scope: True)
    assert client.put("/api/auth/password", json={"password": "another long password"}).status_code in (401, 403)
    assert client.post("/api/auth/open-code").status_code in (401, 403)


def test_changing_the_password_signs_every_device_out(client):
    auth.set_password("first long password")
    auth._new_session("test", "1.2.3.4")
    assert auth.sessions()
    auth.set_password("second long password")
    assert auth.sessions() == []
    auth.clear_password()
    assert not auth.has_password()


def test_expired_code(monkeypatch):
    code = auth.make_code()
    auth._codes[code] -= auth.CODE_SECONDS + 5
    with pytest.raises(auth.AuthError):
        auth.exchange_code(code, "d", "c")
