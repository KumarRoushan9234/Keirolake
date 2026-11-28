"""The MCP tools that let Claude, ChatGPT and other AI apps use the whole lake, and the connector link."""
import json

import pytest
from fastapi.testclient import TestClient

from app import access
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _rpc(client, method, params=None, headers=None, path="/mcp"):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post(path, json=body, headers=headers or {}).json()


def _call(client, name, args, headers=None, path="/mcp"):
    return _rpc(client, "tools/call", {"name": name, "arguments": args}, headers, path)["result"]


def test_tools_carry_annotations_and_instructions(client):
    _, raw = access.create_key("claude", ["read", "ask", "write", "llm"])
    headers = {"Authorization": f"Bearer {raw}"}
    init = _rpc(client, "initialize", {"protocolVersion": "2025-06-18"}, headers)["result"]
    assert "overview" in init["instructions"] and "plan_table" in init["instructions"]
    tools = {t["name"]: t for t in _rpc(client, "tools/list", headers=headers)["result"]["tools"]}
    assert {"overview", "job_status", "capture", "plan_table", "fill_table", "list_models"} <= set(tools)
    assert tools["search"]["annotations"]["readOnlyHint"] is True
    assert tools["capture"]["annotations"]["readOnlyHint"] is False


def test_overview_lists_what_the_key_can_use(client):
    _, raw = access.create_key("reader", ["read"])
    out = _call(client, "overview", {}, {"Authorization": f"Bearer {raw}"})
    data = json.loads(out["content"][0]["text"])
    assert out["isError"] is False and {"collections", "tables", "topics", "tools"} <= set(data)
    assert "capture" not in data["tools"] and data["permissions"] == ["read"]


def test_write_tools_need_the_write_permission(client):
    _, raw = access.create_key("reader", ["read"])
    out = _call(client, "capture", {"text": "hello"}, {"Authorization": f"Bearer {raw}"})
    assert out["isError"] is True and "write" in out["content"][0]["text"]


def test_job_status_only_shows_this_keys_jobs(client):
    _, raw = access.create_key("reader", ["read"])
    out = _call(client, "job_status", {"job_id": "not-a-job"}, {"Authorization": f"Bearer {raw}"})
    assert out["isError"] is True


def test_fill_table_refuses_an_unknown_plan(client):
    _, raw = access.create_key("writer", ["read", "write"])
    out = _call(client, "fill_table", {"session_id": "0" * 32}, {"Authorization": f"Bearer {raw}"})
    assert out["isError"] is True


def test_connector_link_works_and_is_never_logged(client):
    info, raw = access.create_key("chatgpt", ["read"])
    out = _call(client, "overview", {}, path=f"/mcp/k/{raw}")
    assert out["isError"] is False
    assert client.post("/mcp/k/klk_wrong", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    logged = [r["path"] for r in access.access_log()]
    assert "/mcp/k/…" in logged and not any(raw in p for p in logged)
    access.revoke(info["id"])
    assert client.post(f"/mcp/k/{raw}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401


def test_monitor_tools_return_stored_data_and_say_what_is_missing(client):
    import datetime as dt

    from app.db import init_db, session_scope
    from app.monitor import markets, service
    from app.monitor import sources as S
    from app.monitor.models import MarketPrice, MonitorEvent
    from sqlalchemy import delete

    init_db()
    with session_scope() as s:
        s.execute(delete(MarketPrice))
        s.execute(delete(MonitorEvent))
    _, raw = access.create_key("claude-monitor", ["read", "write"])
    h = {"Authorization": f"Bearer {raw}"}
    names = {t["name"] for t in _rpc(client, "tools/list", headers=h)["result"]["tools"]}
    assert {"market_movers", "market_quote", "monitor_events", "threat_lookup", "monitor_alerts", "daily_brief", "write_brief"} <= names

    empty = json.loads(_call(client, "market_movers", {"days": 30}, h)["content"][0]["text"])
    assert empty["ok"] is False and "Turn on" in empty["reason"]  # no invented numbers
    assert "No prices are stored" in _call(client, "market_quote", {"symbol": "TCS"}, h)["content"][0]["text"]
    assert "No brief has been written" in _call(client, "daily_brief", {}, h)["content"][0]["text"]
    assert _call(client, "write_brief", {}, h)["isError"] is True  # nothing to brief on

    today = dt.date.today()
    markets.store_prices([{"symbol": "TCS.NS", "day": (today - dt.timedelta(days=i)).isoformat(), "close": 100.0 + i * 0.5, "prev_close": None, "source": "yahoo", "currency": "INR"}
                          for i in range(40)])
    q = json.loads(_call(client, "market_quote", {"symbol": "tcs"}, h)["content"][0]["text"])
    assert q["symbol"] == "TCS.NS" and q["history_days"] >= 35
    service.store_events(S.parse_kev({"vulnerabilities": [{"cveID": "CVE-2026-9999", "vendorProject": "Acme", "product": "Widget", "vulnerabilityName": "RCE",
                                                          "dateAdded": today.isoformat(), "shortDescription": "x", "requiredAction": "Patch", "dueDate": "", "knownRansomwareCampaignUse": "Known"}]}))
    ev = json.loads(_call(client, "monitor_events", {"group": "cyber", "min_severity": "high"}, h)["content"][0]["text"])
    assert ev and ev[0]["title"].startswith("CVE-2026-9999")
    assert json.loads(_call(client, "threat_lookup", {"term": "acme"}, h)["content"][0]["text"])[0]["severity"] == "critical"
    assert isinstance(json.loads(_call(client, "monitor_alerts", {}, h)["content"][0]["text"]), list)
    _, ro = access.create_key("ro", ["ask"])
    denied = _call(client, "market_movers", {}, {"Authorization": f"Bearer {ro}"})
    assert denied["isError"] is True  # no read permission
