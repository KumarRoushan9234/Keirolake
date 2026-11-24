"""The lake map lists real items with their links, problems and fix actions."""
import pytest
from fastapi.testclient import TestClient

from app.sheets import service


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_map_shows_sheets_with_problems_and_actions(client):
    sheet = service.create_blank("map test sheet", [{"name": "a", "type": "text"}])
    try:
        out = client.get("/api/map").json()
        assert out["layers"][0] == "Sources" and out["totals"]["sheets"] >= 1
        node = next(n for n in out["nodes"] if n["id"] == f"sheet:{sheet['id']}")
        assert node["layer"] == "Lake" and "no rows" in node["issues"][0] and node["status"] == "warn"
        assert {a["label"] for a in node["actions"]} >= {"Open", "Delete sheet"}
        assert next(a for a in node["actions"] if a["label"] == "Delete sheet")["danger"] is True
        ids = {n["id"] for n in out["nodes"]}
        assert all(e["from"] in ids and e["to"] in ids for e in out["edges"])  # no dangling links
        assert {"from": "src:files", "to": node["id"], "label": ""} in out["edges"]
        assert {"model:ollama", "vec:qdrant", "acc:api"} <= ids
    finally:
        service.delete_sheet(sheet["id"])


def test_sheet_chart_groups_the_whole_sheet(client):
    sheet = service.create_blank("chart test", [{"name": "city", "type": "text"}, {"name": "pop", "type": "number"}, {"name": "day", "type": "date"}])
    try:
        sid = sheet["id"]
        service.add_rows(sid, [{"city": "A", "pop": 10, "day": "2026-01-01"}, {"city": "A", "pop": 30, "day": "2026-01-02"},
                               {"city": "B", "pop": 5, "day": "2026-01-03"}, {"city": "C", "pop": None, "day": ""}])
        by_count = client.get(f"/api/sheets/{sid}/chart", params={"x": "city"}).json()
        assert by_count["series"][0] == {"label": "A", "value": 2.0} and by_count["total"] == 4
        total = client.get(f"/api/sheets/{sid}/chart", params={"x": "city", "y": "pop", "agg": "sum"}).json()
        assert total["series"][0] == {"label": "A", "value": 40.0} and total["y"] == "sum of pop"
        hist = client.get(f"/api/sheets/{sid}/chart", params={"x": "pop", "bins": 3}).json()
        assert hist["kind"] == "hist" and sum(p["value"] for p in hist["series"]) == 3
        assert client.get(f"/api/sheets/{sid}/chart", params={"x": "day"}).json()["line"] is True
        assert client.get(f"/api/sheets/{sid}/chart", params={"x": "nope"}).status_code == 400
        assert client.get(f"/api/sheets/{sid}/chart", params={"x": "city", "agg": "sum"}).status_code == 400
    finally:
        service.delete_sheet(sheet["id"])
