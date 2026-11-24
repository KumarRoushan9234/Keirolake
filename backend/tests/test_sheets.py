"""Sheets: create, import, edit, columns, export round-trips, history, AI column fill, API."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from app.llm.registry import LLMResult
from app.sheets import fill, service


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_blank_sheet_edit_rows_and_columns():
    sheet = service.create_blank("People test", [{"name": "Name", "type": "text"}, {"name": "Age", "type": "number"}])
    sid = sheet["id"]
    assert [c["name"] for c in sheet["columns"]] == ["name", "age"]
    ids = service.add_rows(sid, [{"name": "Asha", "age": "31"}, {"name": "Ravi", "age": 40}])
    service.update_cell(sid, ids[0], "age", "32")
    with pytest.raises(service.SheetError):
        service.update_cell(sid, ids[0], "age", "old")
    page = service.rows(sid, sort="age", desc=True)
    assert [r["name"] for r in page["rows"]] == ["Ravi", "Asha"] and page["rows"][1]["age"] == 32.0
    assert service.rows(sid, q="asha")["total"] == 1
    service.add_column(sid, "City")
    service.update_column(sid, "city", new_name="Home town")
    assert "home_town" in [c["name"] for c in service.get(sid)["columns"]]
    service.drop_column(sid, "home_town")
    assert service.delete_rows(sid, [ids[1]]) == 1
    with pytest.raises(service.SheetError):
        service.add_column(sid, "summary", kind="ai")  # AI columns need an instruction
    with pytest.raises(service.SheetError):
        service.create_blank("Bad", [{"name": "City"}, {"name": "city"}])  # duplicate column names
    service.delete_sheet(sid)
    with pytest.raises(KeyError):
        service.get(sid)


def test_import_and_every_export_format(tmp_path):
    csv = b"country,people\nIndia,1450\nNepal,30\n"
    sheet = service.import_file("Population test", "pop.csv", csv)
    sid = sheet["id"]
    assert sheet["rows"] == 2 and sheet["source"] == "import"
    body, media, name = service.export(sid, "csv")
    assert body.decode("utf-8-sig").splitlines()[0] == "country,people" and name.endswith(".csv")
    assert json.loads(service.export(sid, "json")[0])[0]["country"] == "India"
    assert len(service.export(sid, "jsonl")[0].decode().strip().splitlines()) == 2
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(service.export(sid, "xlsx")[0]))
    assert wb.active["A2"].value == "India"
    pdf = service.export(sid, "pdf")[0]
    assert pdf[:5] == b"%PDF-" and len(pdf) > 500
    path = service.save_to_folder(sid, "csv", str(tmp_path))
    assert (tmp_path / path.split("\\")[-1].split("/")[-1]).exists()
    with pytest.raises(service.SheetError):
        service.import_file("x", "doc.pdf", b"%PDF")
    service.snapshot(sid)
    assert service.history(sid)[0]["rows"] == 2
    service.delete_sheet(sid)


@pytest.mark.asyncio
async def test_ai_column_fills_rows_and_keeps_provenance(monkeypatch):
    from app.llm import registry

    sheet = service.create_blank("Capitals test", [{"name": "country", "type": "text"}])
    sid = sheet["id"]
    service.add_rows(sid, [{"country": "France"}, {"country": "Japan"}])
    service.add_column(sid, "capital", kind="ai", prompt="The capital city")
    answers = {"France": "Paris", "Japan": "Tokyo"}

    class Router:
        async def complete(self, messages, **kw):
            country = next(c for c in answers if c in messages[-1].content)
            return LLMResult(json.dumps({"value": answers[country], "confidence": 0.9}), "ollama", "t", [])

    monkeypatch.setattr(registry, "get_router", lambda: Router())
    assert fill.estimate(sid, "capital")["rows"] == 2

    class Ctx:
        async def progress(self, *a):
            pass

    out = await fill.fill_job(sid, "capital")(Ctx())
    assert out["filled"] == 2
    rows = service.rows(sid)["rows"]
    assert {r["country"]: r["capital"] for r in rows} == answers
    assert rows[0]["_meta"]["capital"]["confidence"] == 0.9 and rows[0]["_meta"]["capital"]["model"] == "ollama:t"
    service.update_cell(sid, rows[0]["_id"], "capital", "Paris!")  # a hand edit clears the provenance
    assert "_meta" not in service.rows(sid)["rows"][0]
    service.delete_sheet(sid)


def test_sheet_api_and_public_api(client):
    created = client.post("/api/sheets", json={"name": "Api test", "columns": [{"name": "item"}, {"name": "qty", "type": "number"}]}).json()
    sid = created["id"]
    assert client.post(f"/api/sheets/{sid}/rows", json={"rows": [{"item": "rice", "qty": 2}]}).status_code == 201
    rows = client.get(f"/api/sheets/{sid}/rows").json()
    rid = rows["rows"][0]["_id"]
    assert client.patch(f"/api/sheets/{sid}/rows/{rid}", json={"column": "qty", "value": "5"}).json() == {"ok": True}
    assert client.patch(f"/api/sheets/{sid}/rows/{rid}", json={"column": "qty", "value": "lots"}).status_code == 400
    assert client.get(f"/api/sheets/{sid}/export?format=xlsx").status_code == 200
    assert client.get(f"/api/sheets/{sid}/export?format=exe").status_code == 400
    files = {"file": ("prices.csv", b"thing,price\npen,10\n")}
    imported = client.post("/api/sheets/import", files=files, data={"name": "Prices api"}).json()
    assert imported["rows"] == 1
    assert any(s["id"] == sid for s in client.get("/api/sheets").json())

    from app import access

    _, raw = access.create_key("sheets app", ["read", "write"])
    h = {"Authorization": f"Bearer {raw}"}
    assert any(s["id"] == sid for s in client.get("/v1/sheets", headers=h).json())
    assert client.post(f"/v1/sheets/{sid}/rows", json={"rows": [{"item": "dal", "qty": 1}]}, headers=h).status_code == 201
    assert client.get(f"/v1/sheets/{sid}/rows", headers=h).json()["total"] == 2
    _, ro = access.create_key("read only", ["read"])
    assert client.post(f"/v1/sheets/{sid}/rows", json={"rows": [{"item": "x"}]}, headers={"Authorization": f"Bearer {ro}"}).status_code == 403
    assert client.delete(f"/api/sheets/{sid}").status_code == 204
    client.delete(f"/api/sheets/{imported['id']}")


def test_parse_accepts_json_plain_text_and_refuses_null():
    assert fill._parse('{"value": "Paris", "confidence": 0.8}') == ("Paris", 0.8)
    assert fill._parse("Nairobi") == ("Nairobi", None)
    assert fill._parse("null") == (None, None) and fill._parse('{"value": null}') == (None, None)
    assert fill._parse("A very long rambling answer\nwith several lines") == (None, None)


@pytest.mark.asyncio
async def test_fill_retries_once_when_the_model_answers_null(monkeypatch):
    from app.llm import registry

    sheet = service.create_blank("Retry test", [{"name": "country"}])
    sid = sheet["id"]
    service.add_rows(sid, [{"country": "Kenya"}])
    service.add_column(sid, "capital", kind="ai", prompt="The capital city")
    replies = ['{"value": null, "confidence": 0.2}', '{"value": "Nairobi", "confidence": 0.9}']

    class Router:
        async def complete(self, messages, **kw):
            return LLMResult(replies.pop(0), "ollama", "t", [])

    class Ctx:
        async def progress(self, *a):
            pass

    monkeypatch.setattr(registry, "get_router", lambda: Router())
    out = await fill.fill_job(sid, "capital")(Ctx())
    assert out["filled"] == 1 and service.rows(sid)["rows"][0]["capital"] == "Nairobi"
    service.delete_sheet(sid)


@pytest.mark.asyncio
async def test_web_passages_are_ranked_so_the_number_reaches_the_model(monkeypatch):
    from app.embeddings import chain
    from app.keiro.client import WebResult

    class NoEmbed:
        specs = [object()]

        def available(self):
            return self.specs

        async def embed_with(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(chain, "get_chain", lambda: NoEmbed())
    filler = "History and culture of the islands. " * 120
    page = filler + "The population of Japan is 122 million people in 2025. " + filler
    text, urls = await fill._best_passages("Japan population of the country", [WebResult("https://x.org/japan", "Japan", content=page)])
    assert "122 million" in text and urls == ["https://x.org/japan"]


def test_web_search_uses_only_the_rows_key_column():
    row = {"_id": 1, "country": "Japan", "capital": "Tokyo", "population": None}
    assert fill._identity(row, "population") == "Japan"  # not "Japan Tokyo": the capital would pull in the wrong entity
    assert fill._identity({"_id": 2, "country": None, "capital": "Nairobi"}, "population") == "Nairobi"
    assert fill._identity({"_id": 3}, "x") == ""


def test_ai_columns_may_use_general_knowledge_but_web_columns_stick_to_the_results():
    ai, web = fill.system_prompt(False), fill.system_prompt(True)
    assert "general knowledge" in ai and "use null only if you don't know" in ai
    assert "Use only the row data and the web results" in web and "general knowledge" not in web
