import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health_reports_degraded_without_failing_when_qdrant_is_down(client, monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:9")  # nothing listens here
    from app.vector.qdrant_store import QdrantStore
    import app.vector.qdrant_store as qs
    monkeypatch.setattr(qs, "_store", QdrantStore())
    body = client.get("/api/health").json()
    assert body["embedding_chain"][0] == "openrouter:baai/bge-m3"
    assert body["keiro"]["configured"] is True


def test_providers_lists_fallback_order_and_models(client):
    body = client.get("/api/providers").json()
    assert body["llm_order"] == ["openrouter", "groqcloud"]
    assert body["llm"][0]["models"] == ["model-a", "model-b"]
    assert [e["dim"] for e in body["embeddings"]] == [8, 8]


def test_dataset_upload_query_and_curate_flow(client):
    csv = b"region,amount\nnorth,10\nsouth,20\nnorth,5\n"
    up = client.post("/api/datasets", files={"file": ("orders.csv", csv)}, data={"name": "orders"})
    assert up.status_code == 201 and up.json()["table"] == "ds_orders" and up.json()["rows"] == 3

    q = client.post("/api/warehouse/query", json={"sql": "SELECT region, SUM(amount) t FROM ds_orders GROUP BY region ORDER BY region"})
    assert q.json()["rows"] == [["north", 15], ["south", 20]]

    cur = client.post("/api/warehouse/curated", json={"name": "totals", "sql": "SELECT region, SUM(amount) t FROM ds_orders GROUP BY region"})
    assert cur.status_code == 201 and cur.json()["rows"] == 2

    tables = {t["table"]: t for t in client.get("/api/warehouse/schema").json()}
    assert {"ds_orders", "cur_totals"} <= tables.keys()
    assert {d["zone"] for d in client.get("/api/datasets").json()} >= {"clean", "curated"}


def test_destructive_sql_is_rejected_with_400(client):
    r = client.post("/api/warehouse/query", json={"sql": "DROP TABLE ds_orders"})
    assert r.status_code == 400
    assert client.post("/api/warehouse/query", json={"sql": "SELECT count(*) FROM ds_orders"}).json()["rows"] == [[3]]


def test_bad_dataset_file_is_a_400_not_a_crash(client):
    r = client.post("/api/datasets", files={"file": ("notes.pdf", b"%PDF")})
    assert r.status_code == 400


def test_unknown_knowledge_base_is_404(client):
    assert client.post("/api/kbs/nope/search", json={"query": "x"}).status_code == 404
    assert client.post("/api/kbs/nope/ingest/text", json={"title": "t", "text": "x"}).status_code == 404
    assert client.post("/api/chat", json={"question": "hi", "kb_id": "nope"}).status_code == 404


def test_oversized_upload_is_rejected_with_413(client, monkeypatch):
    import app.config as config
    monkeypatch.setattr(config, "get_settings", lambda: type("S", (), {"max_upload_mb": 1})())
    import app.api.routes as routes
    monkeypatch.setattr(routes, "get_settings", config.get_settings)
    big = b"a,b\n" + b"1,2\n" * (400 * 1024)  # ~1.6 MB
    r = client.post("/api/datasets", files={"file": ("big.csv", big)})
    assert r.status_code == 413


def test_backup_listing_starts_empty(client):
    assert client.get("/api/backups").json() == []


def test_jobs_endpoint_and_stats(client):
    assert client.get("/api/jobs").status_code == 200
    stats = client.get("/api/stats").json()
    assert {z["zone"] for z in stats["zones"]} == {"raw", "clean", "curated"}


def test_create_kb_returns_model_list_in_embedded_qdrant(client, monkeypatch, tmp_path):
    from qdrant_client import AsyncQdrantClient
    from app.vector import qdrant_store as qs
    monkeypatch.setattr(qs, "_store", qs.QdrantStore(AsyncQdrantClient(location=":memory:")))
    res = client.post("/api/kbs", json={"name": "demo kb"})
    assert res.status_code == 201, res.text
    assert res.json()["slug"] == "demo_kb" and isinstance(res.json()["models"], list)
