"""Data API keys, scopes, SQL guard, MCP, webhooks, network gating and backup copies."""
import asyncio
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import access, backup_copy
from app.lake import warehouse
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _key(scopes, **kw):
    info, raw = access.create_key("test app", scopes, **kw)
    return info, {"Authorization": f"Bearer {raw}"}


def test_keys_are_hashed_scoped_and_revocable(client):
    info, headers = _key(["read"])
    assert client.get("/v1/me", headers=headers).json()["scopes"] == ["read"]
    assert client.get("/v1/kbs", headers=headers).status_code == 200
    assert client.post("/v1/sql", json={"sql": "select 1"}, headers=headers).status_code == 403  # no sql scope
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer klk_wrong"}).status_code == 401
    access.revoke(info["id"])
    assert client.get("/v1/me", headers=headers).status_code == 401
    from app.catalog.models import ApiKey
    from app.db import session_scope

    with session_scope() as s:
        stored = s.get(ApiKey, info["id"])
        assert stored.key_hash != headers["Authorization"][7:] and len(stored.key_hash) == 64
    assert any(r["path"] == "/v1/me" for r in access.access_log())


def test_expired_key_is_refused(client):
    info, headers = _key(["read"], expires_days=1)
    from datetime import datetime, timedelta, timezone

    from app.catalog.models import ApiKey
    from app.db import session_scope

    with session_scope() as s:
        s.get(ApiKey, info["id"]).expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    assert client.get("/v1/me", headers=headers).status_code == 401


def test_api_sql_reads_only_user_tables(client):
    from app.db import writer_connection

    w = writer_connection()
    w.execute('CREATE TABLE IF NOT EXISTS "ds_api_test" (city TEXT, people REAL)')
    w.execute('DELETE FROM "ds_api_test"')
    w.executemany('INSERT INTO "ds_api_test" VALUES (?, ?)', [("Pune", 7.0), ("Delhi", 33.0)])
    w.commit(); w.close()
    _, headers = _key(["read", "sql"])
    rows = client.post("/v1/sql", json={"sql": "select city from ds_api_test order by people"}, headers=headers).json()
    assert rows["rows"] == [["Pune"], ["Delhi"]]
    for secret in ("api_keys", "webhooks", "conversations", "documents"):
        r = client.post("/v1/sql", json={"sql": f"select * from {secret}"}, headers=headers)
        assert r.status_code == 400, secret
    assert client.get("/v1/datasets/ds_api_test?format=csv", headers=headers).text.splitlines()[0] == "city,people"
    assert client.get("/v1/datasets/api_keys", headers=headers).status_code == 404
    # in the app itself, the secret tables stay unreadable too
    with pytest.raises(warehouse.SQLValidationError):
        warehouse.run_query("select key_hash from api_keys")


def test_mcp_lists_tools_by_scope_and_calls_them(client):
    _, headers = _key(["read"])
    init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, headers=headers)
    assert init.json()["result"]["serverInfo"]["name"] == "keirolake"
    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers).status_code == 202
    tools = {t["name"] for t in client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers).json()["result"]["tools"]}
    assert "search" in tools and "query_sql" not in tools and "add_document" not in tools
    out = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "list_datasets", "arguments": {}}}, headers=headers).json()["result"]
    assert out["isError"] is False and "ds_api_test" in out["content"][0]["text"]
    denied = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                       "params": {"name": "query_sql", "arguments": {"sql": "select 1"}}}, headers=headers).json()["result"]
    assert denied["isError"] is True


def test_app_api_refuses_other_machines():
    from app import network

    assert network.is_loopback("127.0.0.1") and network.is_loopback("::1") and not network.is_loopback("192.168.1.5")


def test_webhook_signature_and_validation():
    with pytest.raises(access.AccessError):
        access.add_webhook("ftp://x", ["ingest.finished"])
    with pytest.raises(access.AccessError):
        access.add_webhook("https://hooks.test/x", ["nope"])
    hook, secret = access.add_webhook("https://hooks.test/x", ["dataset.created"])
    body = b'{"a":1}'
    import hashlib
    import hmac

    assert access.sign(secret, body) == "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    access.delete_webhook(hook["id"])


def test_rate_limit():
    assert all(access.rate_ok("rate-test", now=100.0 + i * 0.01) for i in range(access.RATE_PER_MINUTE))
    assert not access.rate_ok("rate-test", now=101.5)
    assert access.rate_ok("rate-test", now=200.0)


def _fake_backup(root: Path, stamp: str) -> None:
    folder = root / stamp
    folder.mkdir(parents=True)
    db = sqlite3.connect(folder / "keirolake.db")
    db.execute("CREATE TABLE documents (id TEXT)")
    db.commit(); db.close()
    import gzip

    with gzip.open(folder / "kb_x_v1.snapshot.gz", "wb") as fh:
        fh.write(b"vectors" * 1000)
    (folder / "manifest.json").write_text(json.dumps({"created_at": stamp, "knowledge_bases": [{"name": "x"}], "files": {}}))


def test_encrypted_copy_round_trip_and_restore_test(tmp_path, monkeypatch):
    from app import backup

    monkeypatch.setattr(backup, "backups_dir", lambda: tmp_path / "backups")
    _fake_backup(tmp_path / "backups", "20260101T000000Z")
    target = tmp_path / "usb"
    target.mkdir()
    monkeypatch.setattr(backup_copy, "CHUNK", 1024)  # many chunks
    out = backup_copy.copy_backup("20260101T000000Z", str(target), "correct horse battery", include_lake=False)
    assert out["encrypted"] and out["path"].endswith(".klb")
    assert b"vectors" not in Path(out["path"]).read_bytes()
    report = backup_copy.verify(path=out["path"], passphrase="correct horse battery")
    assert report["ok"], report
    with pytest.raises(backup_copy.BackupCopyError, match="Wrong passphrase"):
        backup_copy.verify(path=out["path"], passphrase="wrong passphrase!")
    data = Path(out["path"]).read_bytes()
    Path(out["path"]).write_bytes(data[:-100])  # cut short
    with pytest.raises(backup_copy.BackupCopyError):
        backup_copy.verify(path=out["path"], passphrase="correct horse battery")
    assert backup_copy.verify(stamp="20260101T000000Z")["ok"]
    plain = backup_copy.copy_backup("20260101T000000Z", str(target), "", include_lake=False)
    assert zipfile.is_zipfile(plain["path"]) and backup_copy.verify(path=plain["path"])["ok"]
    with pytest.raises(backup_copy.BackupCopyError):
        backup_copy.copy_backup("../etc", str(target))


@pytest.mark.asyncio
async def test_network_listener_tags_requests_and_serves_only_the_data_api():
    from app import network

    seen = {}

    async def inner(scope, receive, send):
        seen.update(scope)

    await network.tagged(inner)({"type": "http", "path": "/api/about"}, None, None)
    assert network.is_network_request(seen) and not network.is_network_request({"type": "http"})


def test_network_request_to_app_api_needs_a_signed_in_session(client, monkeypatch):
    from app import network

    monkeypatch.setattr(network, "is_network_request", lambda scope: True)
    assert client.get("/api/about").status_code == 401  # since 0.5 the UI is served here, but /api needs a session
    _, headers = _key(["read"])
    assert client.get("/v1/me", headers=headers).status_code == 200
