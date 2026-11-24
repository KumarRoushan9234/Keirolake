"""/v1/tables and /v1/views, and the MCP table tools: filters stay parameters, writes need 'write', views stay read-only."""
import pytest
from fastapi.testclient import TestClient

from app import access, tables_api
from app.sheets import service


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def table():
    sh = service.create_blank("api cities", [{"name": "city", "type": "text"}, {"name": "pop", "type": "number"}, {"name": "state", "type": "text"}])
    service.add_rows(sh["id"], [{"city": "Pune", "pop": 7, "state": "MH"}, {"city": "Mumbai", "pop": 20, "state": "MH"},
                                {"city": "Delhi", "pop": 30, "state": "DL"}, {"city": "Goa", "pop": None, "state": "GA"}])
    yield sh
    service.delete_sheet(sh["id"])


def key(scopes):
    k, raw = access.create_key("tables test", scopes)
    return k, {"Authorization": f"Bearer {raw}"}


def test_filters_order_and_pages(client, table):
    k, h = key(["read"])
    try:
        url = f"/v1/tables/{table['id']}/rows"
        out = client.get(url, headers=h, params={"where": ["state:eq:MH", "pop:gt:5"], "order": "-pop"}).json()
        assert [r["city"] for r in out["rows"]] == ["Mumbai", "Pune"] and out["total"] == 2
        assert [r["city"] for r in client.get(url, headers=h, params={"where": "city:in:Goa,Delhi", "order": "city"}).json()["rows"]] == ["Delhi", "Goa"]
        assert client.get(url, headers=h, params={"where": "pop:null:true"}).json()["rows"][0]["city"] == "Goa"
        assert client.get(url, headers=h, params={"where": "city:like:%u%", "limit": 1}).json()["total"] == 2
        assert "Pune" in client.get(url, headers=h, params={"format": "csv"}).text
    finally:
        access.revoke(k["id"])


def test_filters_cannot_inject(client, table):
    k, h = key(["read"])
    try:
        url = f"/v1/tables/{table['id']}/rows"
        assert client.get(url, headers=h, params={"where": 'city" OR 1=1 --:eq:x'}).status_code == 400
        assert client.get(url, headers=h, params={"where": "city:eq:x' OR '1'='1"}).json()["total"] == 0  # a value, not SQL
        assert client.get(url, headers=h, params={"order": "pop; DROP TABLE x"}).status_code == 400
        assert client.get(url, headers=h, params={"where": "city:regex:x"}).status_code == 400
    finally:
        access.revoke(k["id"])


def test_writes_need_write_scope(client, table):
    k, h = key(["read"])
    kw, hw = key(["read", "write"])
    try:
        url = f"/v1/tables/{table['id']}/rows"
        assert client.post(url, headers=h, json={"rows": [{"city": "X"}]}).status_code == 403
        rid = client.post(url, headers=hw, json={"rows": [{"city": "Nagpur", "pop": 3, "state": "MH"}]}).json()["ids"][0]
        assert client.patch(f"{url}/{rid}", headers=hw, json={"pop": 4}).json()["pop"] == 4
        assert client.delete(f"{url}/{rid}", headers=hw).status_code == 204
        assert client.delete(f"{url}/{rid}", headers=hw).status_code == 404
        made = client.post("/v1/tables", headers=hw, json={"name": "api made", "columns": [{"name": "a", "type": "text"}]})
        assert made.status_code == 201
        service.delete_sheet(made.json()["id"])
    finally:
        access.revoke(k["id"]); access.revoke(kw["id"])


def test_saved_views_are_read_only(client, table):
    k, h = key(["read", "sql"])
    try:
        sql = f'SELECT state, SUM(pop) AS people FROM "{table["table"]}" GROUP BY state ORDER BY state'
        assert client.post("/v1/views", headers=h, json={"name": "people_by_state", "sql": sql}).status_code == 201
        assert client.get("/v1/views/people_by_state", headers=h).json()["rows"] == [["DL", 30.0], ["GA", None], ["MH", 27.0]]
        assert client.post("/v1/views", headers=h, json={"name": "evil", "sql": f'DELETE FROM "{table["table"]}"'}).status_code == 400
        assert client.post("/v1/views", headers=h, json={"name": "keys", "sql": "SELECT * FROM api_keys"}).status_code == 400
        assert client.post("/v1/views", headers=h, json={"name": "Bad Name", "sql": sql}).status_code == 400
        assert client.delete("/v1/views/people_by_state", headers=h).status_code == 204
    finally:
        access.revoke(k["id"])


@pytest.mark.asyncio
async def test_rows_found_by_meaning(table, monkeypatch):
    from app.rag import retrieve as rv

    async def fake_retrieve(kb_id, q, top_k=8, **kw):
        return rv.RetrievalResult([rv.Hit("c", "d", "row", "row:3", "city: Delhi", 1.0, 0.9)], 0.9, [])

    monkeypatch.setattr(rv, "retrieve", fake_retrieve)
    monkeypatch.setattr(tables_api, "index_info", lambda sid: {"kb_id": "kb", "rows": 4, "indexed_at": ""})
    found = await tables_api.search(table["id"], "capital")
    assert found[0]["city"] == "Delhi" and found[0]["_score"] == 0.9
    assert tables_api.row_text({"_id": 1, "city": "Pune", "pop": None}) == "city: Pune"


def test_mcp_table_tools(client, table):
    k, h = key(["read"])
    try:
        names = [t["name"] for t in client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]]
        assert {"list_tables", "read_rows", "search_table", "list_topics"} <= set(names) and "insert_rows" not in names
        call = lambda n, a: client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": n, "arguments": a}}).json()["result"]
        out = call("read_rows", {"table": table["id"], "where": ["state:eq:DL"]})
        assert not out["isError"] and "Delhi" in out["content"][0]["text"]
        assert call("insert_rows", {"table": table["id"], "rows": [{"city": "X"}]})["isError"]  # needs write
    finally:
        access.revoke(k["id"])


def test_app_side_files_views_and_gateway(client, table):
    out = client.get("/api/lake/files").json()
    assert set(out["zones"]) == {"raw", "clean", "curated"}
    assert client.get("/api/lake/files", params={"path": "../.."}).status_code == 400
    assert client.get("/api/lake/file", params={"path": "../keirolake.db"}).status_code == 400
    sql = f'SELECT city FROM "{table["table"]}"'
    assert client.post("/api/views", json={"name": "ui_view", "sql": sql}).status_code == 201
    assert any(v["name"] == "ui_view" for v in client.get("/api/views").json())
    assert client.delete("/api/views/ui_view").status_code == 204
    assert client.get(f"/api/sheets/{table['id']}/meaning", params={"q": "x"}).status_code == 409  # not indexed yet
    g = client.get("/api/gateway").json()
    assert g["models"][:2] == ["auto", "lake"]


def test_deleting_a_table_removes_its_search_collection(client, monkeypatch):
    from app.catalog.models import TableIndex
    from app.db import session_scope

    sh = service.create_blank("indexed then deleted", [{"name": "a", "type": "text"}])
    gone = []

    async def fake_delete(kb_id):
        gone.append(kb_id)

    monkeypatch.setattr("app.catalog.kb.delete_kb", fake_delete)
    with session_scope() as s:
        s.add(TableIndex(sheet_id=sh["id"], kb_id="kb-of-table", rows=1))
    assert client.delete(f"/api/sheets/{sh['id']}").status_code == 204
    assert gone == ["kb-of-table"] and tables_api.index_info(sh["id"]) is None
