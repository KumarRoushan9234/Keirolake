"""Open-port mode: allow-list, per-visitor rate limit, password first, safe settings, and the UPnP client."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import network, upnp


def test_allow_list_and_visitor(monkeypatch):
    monkeypatch.setenv("LAN_ALLOW", "203.0.113.0/24, 10.0.0.5, junk")
    assert [str(n) for n in network.allow_list()] == ["203.0.113.0/24", "10.0.0.5/32"]
    assert network.permitted("203.0.113.9") and network.permitted("10.0.0.5") and not network.permitted("198.51.100.1")
    assert network.permitted("127.0.0.1")  # this computer is always allowed
    scope = {"client": ("127.0.0.1", 1), "headers": [(b"cf-connecting-ip", b"198.51.100.7")]}
    assert network.visitor(scope) == "198.51.100.7" and not network.permitted(network.visitor(scope))
    assert network.visitor({"client": ("192.168.1.4", 1), "headers": [(b"cf-connecting-ip", b"1.2.3.4")]}) == "192.168.1.4"


def test_rate_limit_per_visitor(monkeypatch):
    monkeypatch.setattr(network, "RATE_PER_MINUTE", 3)
    network._hits.clear()
    assert all(network.rate_ok("9.9.9.9", now=100 + i) for i in range(3))
    assert not network.rate_ok("9.9.9.9", now=104) and network.rate_ok("8.8.8.8", now=104)
    assert network.rate_ok("9.9.9.9", now=200)  # a minute later


def test_listener_refuses_before_routing(monkeypatch):
    monkeypatch.setenv("LAN_ALLOW", "203.0.113.0/24")
    sent = []

    async def app(scope, receive, send):
        sent.append("reached")

    async def send(msg):
        sent.append(msg)

    asyncio.run(network.tagged(app)({"type": "http", "client": ("198.51.100.1", 5), "headers": []}, None, send))
    assert sent[0]["status"] == 403 and "reached" not in sent


def test_bind_address(monkeypatch):
    monkeypatch.setenv("LAN_ACCESS", "open")
    monkeypatch.setenv("LAN_BIND", "not-an-ip")
    assert network.bind_address() == "0.0.0.0"
    monkeypatch.setenv("LAN_BIND", "192.168.1.20")
    assert network.bind_address() == "192.168.1.20"
    monkeypatch.setenv("LAN_ACCESS", "local")
    assert network.bind_address() == "127.0.0.1"


def test_settings_are_checked(monkeypatch):
    from app import auth
    from app.main import app

    with TestClient(app) as c:
        monkeypatch.setattr(auth, "has_password", lambda: False)
        assert c.put("/api/access/network", json={"mode": "open", "port": 18555}).status_code == 400  # password first
        monkeypatch.setattr(auth, "has_password", lambda: True)
        assert c.put("/api/access/network", json={"mode": "off", "port": 18555, "bind": "8.8.8.8"}).status_code == 400
        assert c.put("/api/access/network", json={"mode": "off", "port": 18555, "allow": ["not a range"]}).status_code == 400
        assert c.put("/api/access/network", json={"mode": "off", "port": 18555, "cert": "C:/nope.pem", "key": "C:/nope.key"}).status_code == 400
        ok = c.put("/api/access/network", json={"mode": "off", "port": 8443, "allow": [], "bind": "", "cert": "", "key": "", "upnp": False})
        assert ok.status_code == 200 and ok.json()["allow"] == []


DESC = b"""<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device><deviceList><device><deviceList><device>
<serviceList><service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
<controlURL>/ctl/IPConn</controlURL></service></serviceList></device></deviceList></device></deviceList></device></root>"""


def test_upnp_description_and_soap():
    service, control = upnp.parse_description(DESC, "http://192.168.1.1:5000/rootDesc.xml")
    assert service.endswith("WANIPConnection:1") and control == "http://192.168.1.1:5000/ctl/IPConn"
    body = upnp.soap_body(service, "AddPortMapping", {"NewExternalPort": 8443, "NewProtocol": "TCP"})
    assert "<u:AddPortMapping" in body and "<NewExternalPort>8443</NewExternalPort>" in body
    assert upnp.parse_description(b"<root><service><serviceType>other</serviceType></service></root>", "http://x/") is None


@pytest.mark.asyncio
async def test_upnp_without_a_router(monkeypatch):
    monkeypatch.setattr(upnp, "_discover_sync", lambda timeout=3.0: [])
    with pytest.raises(upnp.UPnPError, match="No router"):
        await upnp.add_mapping(8443)
