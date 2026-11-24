"""Staying reachable: the sleep blocker, the lid setting (with the old values put back), and the tunnel's own listener."""
import json

import pytest
from fastapi.testclient import TestClient

from app import power, tunnel


def test_lid_change_remembers_and_restores(monkeypatch, tmp_path):
    monkeypatch.setattr(power.sys, "platform", "win32")
    monkeypatch.setattr(power, "_state_file", lambda: tmp_path / "power-lid.json")
    plan = {"ac": 1, "dc": 1}
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "/qh":
            return f"Current AC Power Setting Index: 0x{plan['ac']:08x}\nCurrent DC Power Setting Index: 0x{plan['dc']:08x}\n"
        if args[0] == "/setacvalueindex":
            plan["ac"] = int(args[-1])
        if args[0] == "/setdcvalueindex":
            plan["dc"] = int(args[-1])
        return ""

    monkeypatch.setattr(power, "_powercfg", fake)
    assert power.keep_on_with_lid_closed(True) == {"ac": 0, "dc": 1}  # plugged in only, by default
    assert json.loads((tmp_path / "power-lid.json").read_text()) == {"ac": 1, "dc": 1}
    assert power.keep_on_with_lid_closed(False) == {"ac": 1, "dc": 1} and not (tmp_path / "power-lid.json").exists()
    assert ("/setactive", "SCHEME_CURRENT") in calls


def test_linux_lid_explains_the_admin_step(monkeypatch):
    monkeypatch.setattr(power.sys, "platform", "linux")
    with pytest.raises(power.PowerError, match="HandleLidSwitch"):
        power.keep_on_with_lid_closed(True)


def test_stay_awake_thread_starts_and_stops(monkeypatch):
    monkeypatch.setattr(power.sys, "platform", "win32")
    held = []
    monkeypatch.setattr(power, "_windows_hold", lambda stop: (held.append(1), stop.wait()))
    power.stay_awake(True)
    assert power.awake_on()
    power.stay_awake(False)
    assert not power.awake_on() and held == [1]


def test_named_tunnel_settings(monkeypatch):
    from app.main import app

    monkeypatch.setattr(tunnel, "running", lambda: False)
    with TestClient(app) as c:
        assert c.put("/api/network/tunnel/config", json={"token": "short"}).status_code == 400
        assert c.put("/api/network/tunnel/config", json={"hostname": "not a host"}).status_code == 400
        ok = c.put("/api/network/tunnel/config", json={"token": "A" * 120, "hostname": "https://Lake.Example.com/", "port": 18442}).json()
        assert ok["named"] and ok["hostname"] == "lake.example.com" and ok["port"] == 18442 and ok["provider"].endswith("named tunnel")
        c.put("/api/network/tunnel/config", json={"token": "", "hostname": "", "port": 8442})
        assert c.get("/api/network/tunnel").json()["named"] is False


@pytest.mark.asyncio
async def test_tunnel_has_its_own_listener_and_uses_the_token_from_the_environment(monkeypatch, tmp_path):
    seen = {}

    class Proc:
        returncode = None
        pid = 1
        stderr = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*args, **kw):
        seen.update(args=args, env=kw["env"])
        return Proc()

    async def ready(proc):
        return "https://lake.example.com"

    started = []

    async def listener():
        started.append(1)

    monkeypatch.setattr(tunnel.auth, "has_password", lambda: True)
    monkeypatch.setattr(tunnel, "binary", lambda: tmp_path / "cloudflared.exe")
    (tmp_path / "cloudflared.exe").write_bytes(b"x")
    monkeypatch.setattr(tunnel.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tunnel, "_read_ready", ready)
    monkeypatch.setattr(tunnel, "_start_listener", listener)
    monkeypatch.setattr(tunnel, "_kill_strays", lambda: 0)
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_TOKEN", "T" * 80)
    monkeypatch.setenv("TUNNEL_HOSTNAME", "lake.example.com")
    try:
        out = await tunnel.start()
        assert started == [1] and out["url"] == "https://lake.example.com"
        assert "T" * 80 not in " ".join(map(str, seen["args"])) and seen["env"]["TUNNEL_TOKEN"] == "T" * 80  # never on the command line
    finally:
        await tunnel.stop(remember=False)
