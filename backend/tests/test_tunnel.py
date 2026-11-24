"""The one-switch tunnel: checksum-verified download, password first, desktop-only control, per-visitor lockout."""
import hashlib

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import tunnel

pytestmark = pytest.mark.asyncio


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(tunnel, "binary", lambda: tmp_path / "bin" / "cloudflared.exe")
    monkeypatch.setattr(tunnel.sys, "platform", "win32")
    return tmp_path


def release(body: bytes, digest: str | None = None):
    sha = digest or hashlib.sha256(body).hexdigest()
    return {"assets": [{"name": "cloudflared-windows-amd64.exe", "browser_download_url": "https://dl.example/cf.exe", "digest": f"sha256:{sha}"}]}


@respx.mock
async def test_install_verifies_the_checksum(data):
    body = b"MZ-fake-binary"
    respx.get(tunnel.RELEASES).mock(return_value=httpx.Response(200, json=release(body)))
    respx.get("https://dl.example/cf.exe").mock(return_value=httpx.Response(200, content=body))
    await tunnel.install()
    assert tunnel.binary().read_bytes() == body


@respx.mock
async def test_a_file_that_does_not_match_is_discarded(data):
    respx.get(tunnel.RELEASES).mock(return_value=httpx.Response(200, json=release(b"good")))
    respx.get("https://dl.example/cf.exe").mock(return_value=httpx.Response(200, content=b"tampered"))
    with pytest.raises(tunnel.TunnelError, match="checksum"):
        await tunnel.install()
    assert not tunnel.binary().exists()


@respx.mock
async def test_no_published_checksum_means_no_download(data):
    respx.get(tunnel.RELEASES).mock(return_value=httpx.Response(200, json={"assets": [{"name": "cloudflared-windows-amd64.exe", "browser_download_url": "https://dl.example/cf.exe"}]}))
    with pytest.raises(tunnel.TunnelError, match="checksum"):
        await tunnel.install()


async def test_start_requires_a_password(data, monkeypatch):
    monkeypatch.setattr(tunnel.auth, "has_password", lambda: False)
    with pytest.raises(tunnel.TunnelError, match="password"):
        await tunnel.start()


def test_url_is_found_in_tunnel_output():
    line = "2026-11-18T08:00:00Z INF |  https://quiet-river-demo-1234.trycloudflare.com  |"
    assert tunnel.URL_RE.search(line).group(0) == "https://quiet-river-demo-1234.trycloudflare.com"


def test_remote_visitors_cannot_control_the_tunnel(monkeypatch):
    from app import network
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/network/tunnel").json()["provider"].startswith("Cloudflare")
        monkeypatch.setattr(network, "is_network_request", lambda scope: True)
        assert c.post("/api/network/tunnel/start").status_code in (401, 403)  # no session: refused before the route
        assert c.put("/api/network", json={"mode": "lan", "port": 8443}).status_code in (401, 403)


def test_lockout_uses_the_visitor_address_behind_the_tunnel():
    from starlette.requests import Request

    from app.api.auth_routes import _client

    def req(client, headers):
        return Request({"type": "http", "client": (client, 1), "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})

    assert _client(req("127.0.0.1", {"cf-connecting-ip": "203.0.113.9"})) == "203.0.113.9"
    assert _client(req("192.168.1.5", {"cf-connecting-ip": "203.0.113.9"})) == "192.168.1.5"  # not trusted from other machines
