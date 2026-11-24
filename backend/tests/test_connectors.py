"""Connectors fill sheets from JSON, feeds and folders; the map keeps the user's own layout."""
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import connectors, lakemap
from app.sheets import service

pytestmark = pytest.mark.asyncio

FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>First</title><link>https://n.example.org/1</link><pubDate>Mon, 01 Jan 2026</pubDate><description>a</description></item>
<item><title>Second</title><link>https://n.example.org/2</link></item></channel></rss>"""


def test_json_records_are_found_and_flattened():
    data = json.dumps({"data": {"items": [{"id": 1, "name": "A", "geo": {"lat": 1.5}}, {"id": 2, "name": "B", "tags": ["x"]}]}}).encode()
    rows = connectors.rows_from_json(data, "data.items")
    assert rows[0] == {"id": 1, "name": "A", "geo_lat": 1.5} and rows[1]["tags"] == '["x"]'
    assert connectors.rows_from_json(json.dumps({"results": [{"a": 1}]}).encode())[0] == {"a": 1}  # a single list is found on its own
    with pytest.raises(connectors.ConnectorError, match="Couldn't find"):
        connectors.rows_from_json(data, "data.nope")
    with pytest.raises(connectors.ConnectorError, match="JSON"):
        connectors.rows_from_json(b"<html>")


def test_feed_rows_and_unsafe_xml():
    rows = connectors.rows_from_feed(FEED)
    assert rows[0]["title"] == "First" and rows[0]["published"].startswith("Mon") and rows[1]["link"].endswith("/2")
    with pytest.raises(connectors.ConnectorError, match="aren't allowed"):
        connectors.rows_from_feed(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss><item><title>&a;</title></item></rss>')


def test_addresses_must_be_public(monkeypatch):
    with pytest.raises(connectors.ConnectorError, match="Local and private"):
        connectors.validate("rest", {"url": "http://127.0.0.1:8000/api/sheets"}, 0, False)
    with pytest.raises(connectors.ConnectorError, match="desktop"):
        connectors.validate("folder", {"path": "."}, 0, False)
    with pytest.raises(connectors.ConnectorError, match="15"):
        connectors.validate("rest", {"url": "https://example.org"}, 5, False)


async def test_run_updates_by_key_and_replaces_without_one(tmp_path, monkeypatch):
    monkeypatch.setattr(connectors.capture, "check_public_url", lambda u: u)
    c = connectors.create("conn test", "rest", {"url": "https://api.example.org/x", "json_path": ""}, key_column="id")
    payloads = [[{"id": 1, "price": 10}, {"id": 2, "price": 20}], [{"id": 2, "price": 25}, {"id": 3, "price": 30}]]

    class Ctx:
        async def progress(self, *a):
            pass

    try:
        with respx.mock:
            route = respx.get("https://api.example.org/x")
            route.mock(side_effect=lambda r: httpx.Response(200, json=payloads.pop(0)))
            first = await connectors.run_job(c["id"])(Ctx())
            assert first["added"] == 2
            second = await connectors.run_job(c["id"])(Ctx())
        assert second["added"] == 1 and second["updated"] == 1
        rows = {r["id"]: r for r in service.rows(second["sheet_id"])["rows"]}
        assert set(rows) == {1, 2, 3} and rows[2]["price"] == 25 and rows[1]["price"] == 10
        assert connectors.get(c["id"])["last_result"]["added"] == 1
    finally:
        sid = connectors.get(c["id"])["sheet_id"]
        connectors.delete(c["id"])
        if sid:
            service.delete_sheet(sid)


async def test_folder_reads_only_new_files(tmp_path):
    (tmp_path / "a.csv").write_text("city,pop\nX,1\nY,2\n")
    c = connectors.create("folder test", "folder", {"path": str(tmp_path)}, local_ok=True)

    class Ctx:
        async def progress(self, *a):
            pass

    try:
        first = await connectors.run_job(c["id"])(Ctx())
        assert first["added"] == 2
        again = await connectors.run_job(c["id"])(Ctx())
        assert again["note"] == "No new files"
    finally:
        sid = connectors.get(c["id"])["sheet_id"]
        connectors.delete(c["id"])
        if sid:
            service.delete_sheet(sid)


def test_map_state_is_cleaned_and_applied():
    from app.main import app

    with TestClient(app) as client:
        bad = client.put("/api/map/state", json={"custom": [{"id": "custom:1", "kind": "script", "layer": "Access"}]})
        assert bad.status_code == 400
        saved = client.put("/api/map/state", json={
            "positions": {"src:web": {"x": 10, "y": 20}}, "hidden": ["src:open"],
            "custom": [{"id": "custom:1", "kind": "app", "layer": "Access", "label": "My dashboard"}],
            "links": [{"from": "acc:api", "to": "custom:1", "label": "reads"}, {"from": "nope", "to": "custom:1"}]})
        assert saved.status_code == 200
        try:
            m = client.get("/api/map").json()
            by = {n["id"]: n for n in m["nodes"]}
            assert by["src:web"]["pos"] == {"x": 10.0, "y": 20.0} and "src:open" not in by and m["hidden"][0]["id"] == "src:open"
            assert by["custom:1"]["custom"] and {"from": "acc:api", "to": "custom:1", "label": "reads", "custom": True} in m["edges"]
            assert not any(e["from"] == "nope" for e in m["edges"])
        finally:
            client.put("/api/map/state", json={})


def test_nat64_addresses_are_judged_by_their_ipv4(monkeypatch):
    import socket

    from app import capture

    def fake(host, port, *a, **k):
        return [(socket.AF_INET6, 0, 0, "", (addr[host], 0, 0, 0))]

    addr = {"pub.example": "64:ff9b::14cf:4955", "lan.example": "64:ff9b::c0a8:0101", "map.example": "::ffff:127.0.0.1"}
    monkeypatch.setattr(capture.socket, "getaddrinfo", fake)
    assert capture.check_public_url("https://pub.example/x")
    for host in ("lan.example", "map.example"):
        with pytest.raises(capture.CaptureError):
            capture.check_public_url(f"https://{host}/x")


async def test_fields_that_clean_to_the_same_column_name_are_both_kept():
    c = connectors.create("dup test", "folder", {"path": "."}, local_ok=True)
    try:
        out = connectors._write(c, [{"+1": 3, "-1": 5, "id": 1}, {"+1": 4, "-1": 6, "id": 2}])
        cols = [col["name"] for col in service.get(out["sheet_id"])["columns"]]
        assert len(cols) == len(set(cols)) == 3 and out["added"] == 2
        sid = out["sheet_id"]
    finally:
        connectors.delete(c["id"])
    service.delete_sheet(sid)
