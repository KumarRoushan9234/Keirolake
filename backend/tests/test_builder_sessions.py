"""Build-with-AI sessions live in the engine: a reply or a fill finishes and is saved even if no page is watching."""
import json

import pytest
from fastapi.testclient import TestClient

from app import builder, builder_sessions as sessions
from app.llm.registry import LLMResult
from app.main import app
from app.sheets import service


class Router:
    async def complete(self, messages, **kw):
        system = messages[0].content
        if "by conversation" in system:
            return LLMResult(json.dumps({"reply": "Name and capital. Shall I fill them?", "ready": True,
                                         "draft": {"name": "qa-session countries", "columns": [{"name": "country"}, {"name": "capital"}], "rows": 3}}),
                             "ollama", "m", [])
        return LLMResult('[{"country": "India", "capital": "New Delhi"}, {"country": "Nepal", "capital": "Kathmandu"}]', "ollama", "m", [])


class Ctx:
    async def progress(self, *a):
        pass


@pytest.fixture
def fake_model(monkeypatch):
    monkeypatch.setattr(builder, "_router", lambda: Router())

    async def no_context(*a):
        return "", []

    monkeypatch.setattr(builder, "_context", no_context)


@pytest.mark.asyncio
async def test_a_session_keeps_the_conversation_and_what_was_built(fake_model):
    s = sessions.create("table")
    try:
        job = sessions.say_job(s["id"], "Countries with their capitals")
        assert sessions.get(s["id"])["busy"] == "thinking"  # the message is saved before the model answers
        with pytest.raises(sessions.SessionError):
            sessions.say_job(s["id"], "again")  # one step at a time
        await job(Ctx())
        got = sessions.get(s["id"])
        assert got["busy"] == "" and got["ready"] and [c["name"] for c in got["draft"]["columns"]] == ["country", "capital"]
        assert [m["role"] for m in got["messages"]] == ["user", "assistant"]

        sessions.set_draft(s["id"], {**got["draft"], "columns": got["draft"]["columns"] + [{"name": "Region", "type": "text"}]})
        job, _ = sessions.fill_job(s["id"], 2)
        out = await job(Ctx())
        got = sessions.get(s["id"])
        assert out["added"] == 2 and got["built"]["id"] == out["sheet_id"] and "Done: 2 rows" in got["messages"][-1]["content"]
        assert "region" in [c["name"] for c in service.get(out["sheet_id"])["columns"]]
        assert any(x["id"] == s["id"] and x["title"] == "qa-session countries" for x in sessions.list_sessions())
    finally:
        b = sessions.get(s["id"]).get("built")
        if b:
            service.delete_sheet(b["id"])
        sessions.delete(s["id"])


def test_a_session_can_start_from_an_existing_table_and_recovers_after_a_restart():
    sheet = service.create_blank("qa-session existing", [{"name": "city", "type": "text"}, {"name": "pop", "type": "number"}])
    s = sessions.create("table", service.get(sheet["id"]))
    try:
        assert s["built"]["id"] == sheet["id"] and [c["name"] for c in s["draft"]["columns"]] == ["city", "pop"] and s["ready"]
        sessions._change(s["id"], lambda x: x.update(busy="filling"))
        sessions.recover()
        got = sessions.get(s["id"])
        assert got["busy"] == "" and "restarted" in got["error"]
    finally:
        sessions.delete(s["id"])
        service.delete_sheet(sheet["id"])


def test_session_routes():
    c = TestClient(app)
    s = c.post("/api/builder/sessions", json={"kind": "collection"}).json()
    try:
        assert c.put(f"/api/builder/sessions/{s['id']}/options", json={"web": True}).json()["options"]["web"] is True
        d = c.put(f"/api/builder/sessions/{s['id']}/draft", json={"name": "Birds", "sections": ["Crows", "Owls"]}).json()
        assert d["ready"] and d["draft"]["sections"] == ["Crows", "Owls"]
        assert c.get("/api/builder/sessions/not-a-session").status_code == 404
        assert c.post(f"/api/builder/sessions/{s['id']}/say", json={"text": ""}).status_code == 422
    finally:
        assert c.delete(f"/api/builder/sessions/{s['id']}").status_code == 204
