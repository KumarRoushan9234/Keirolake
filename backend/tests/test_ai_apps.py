"""Connecting AI apps: per-app keys, ready-made setups, Claude Desktop's config, the tool reference and the logs."""
import json
import sys

import pytest
from fastapi.testclient import TestClient

from app import access, ai_apps
from app.config import get_settings
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    exe = tmp_path / "KeiroLake" / "keirolake-desktop.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    config = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setenv("KEIROLAKE_APP_EXE", str(exe))
    monkeypatch.setattr(ai_apps, "claude_config_path", lambda: config)
    return exe, config


def test_setups_use_the_bridge_and_a_key_per_app(desktop):
    exe, _ = desktop
    out = ai_apps.create("claude-code", None, "")
    assert out["key"]["name"] == "Claude Code" and set(out["key"]["scopes"]) == set(ai_apps.DEFAULT_SCOPES)
    assert out["setups"]["claude-code"].endswith(" mcp") and str(exe) in out["setups"]["claude-code"]
    server = json.loads(out["setups"]["claude-desktop"])["mcpServers"]["keirolake"]
    assert server == {"command": str(exe), "args": ["mcp"], "env": {"KEIROLAKE_KEY": out["raw_key"]}}
    assert out["setups"]["connector_url"] == ""  # no internet link yet


def test_connector_link_when_the_internet_link_is_on(desktop):
    out = ai_apps.create("chatgpt", ["read"], "https://lake.example.com")
    assert out["setups"]["connector_url"] == f"https://lake.example.com/mcp/k/{out['raw_key']}"
    assert json.loads(out["setups"]["remote"])["mcpServers"]["keirolake"]["headers"]["Authorization"].startswith("Bearer klk_")


def test_claude_desktop_config_is_merged_with_a_backup(desktop):
    _, config = desktop
    config.parent.mkdir()
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}), encoding="utf-8")
    out = ai_apps.create("claude-desktop", None, "")
    ai_apps.add_to_claude_desktop(out["raw_key"])
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["theme"] == "dark" and "other" in saved["mcpServers"]
    assert saved["mcpServers"]["keirolake"]["env"]["KEIROLAKE_KEY"] == out["raw_key"]
    assert len(list(config.parent.glob("claude_desktop_config.backup-*.json"))) == 1
    assert ai_apps.claude_desktop_status()["connected"] is True


def test_broken_claude_config_is_left_alone(desktop):
    _, config = desktop
    config.parent.mkdir()
    config.write_text("{not json", encoding="utf-8")
    with pytest.raises(ai_apps.ConnectError):
        ai_apps.add_to_claude_desktop("klk_x")
    assert config.read_text(encoding="utf-8") == "{not json"


def test_without_the_desktop_app_nothing_is_written(monkeypatch, tmp_path):
    monkeypatch.delenv("KEIROLAKE_APP_EXE", raising=False)
    monkeypatch.setattr(ai_apps, "claude_config_path", lambda: tmp_path / "c.json")
    with pytest.raises(ai_apps.ConnectError):
        ai_apps.add_to_claude_desktop("klk_x")
    assert not (tmp_path / "c.json").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
def test_store_version_of_claude_is_found(monkeypatch, tmp_path):
    store = tmp_path / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ai_apps.claude_config_path() == store / "claude_desktop_config.json"


def test_connect_endpoint_and_tool_reference(client, desktop):
    out = client.post("/api/connect", json={"app": "cursor", "scopes": ["read"]}).json()
    assert out["raw_key"].startswith("klk_") and "cursor" in out["setups"]
    assert client.post("/api/connect", json={"app": "nope"}).status_code == 400
    tools = {t["name"]: t for t in client.get("/api/connect/tools").json()}
    assert tools["overview"]["read_only"] is True and tools["plan_table"]["permission"] == "write"
    access.revoke(out["key"]["id"])


def test_logs_are_redacted_and_limited_to_known_files(client):
    logs = get_settings().data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "engine.log").write_text("started\nAuthorization: Bearer klk_secretsecret123\n", encoding="utf-8")
    text = client.get("/api/connect/logs/engine").json()["text"]
    assert "started" in text and "secretsecret" not in text
    assert client.get("/api/connect/logs/..%2Fsecrets").status_code == 404
    assert client.get("/api/connect/logs/vault").status_code == 404
