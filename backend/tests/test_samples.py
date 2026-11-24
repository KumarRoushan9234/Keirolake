"""Sample data: added once to an empty lake, removable, never re-added after that."""
import pytest

from app import samples, tables_api
from app.sheets import service

pytestmark = pytest.mark.asyncio


async def test_samples_add_and_remove(monkeypatch):
    monkeypatch.delenv("SAMPLES_SEEDED", raising=False)
    jobs = []

    async def fake_create(name, desc, *a, **k):
        raise RuntimeError("vector store off in this test")

    monkeypatch.setattr("app.catalog.kb.create_kb", fake_create)
    st = await samples.add(lambda *a: jobs.append(a))
    try:
        info = service.get(st["table_id"])
        assert info["rows"] == 217 and info["attribution"].startswith("World Bank")
        cols = {c["name"]: c["type"] for c in info["columns"]}
        assert cols["population"] == "number" and cols["life_expectancy"] == "number"
        view = tables_api.run_view(samples.VIEW)
        assert view["rows"] and view["columns"][0] == "region"
        assert samples.status()["seeded"] is True
    finally:
        out = await samples.remove()
    assert out["table_id"] is None and not any(v["name"] == samples.VIEW for v in tables_api.list_views())


async def test_first_run_only_when_the_lake_is_empty(monkeypatch):
    called = []

    async def fake_add(submit=None):
        called.append(1)

    monkeypatch.setattr(samples, "add", fake_add)
    monkeypatch.delenv("KEIROLAKE_NO_SAMPLES", raising=False)
    monkeypatch.setenv("SAMPLES_SEEDED", "1")
    await samples.seed_first_run()
    assert called == []  # already seeded once
    monkeypatch.delenv("SAMPLES_SEEDED", raising=False)
    monkeypatch.setattr(samples, "_empty_lake", lambda: True)
    await samples.seed_first_run()
    assert called == [1]
