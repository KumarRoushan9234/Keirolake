"""Automatic backups, search across offline libraries and source setup status."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.llm.registry import LLMResult


# ---------- automatic backups ----------
def test_backup_schedule_and_pruning(tmp_path, monkeypatch):
    import app.backup as backup

    now = datetime(2026, 5, 10, 12, tzinfo=timezone.utc)
    assert backup.backup_due(None, 24, now) is True
    assert backup.backup_due(now - timedelta(hours=23), 24, now) is False
    assert backup.backup_due(now - timedelta(hours=25), 24, now) is True
    assert backup.backup_due(None, 0, now) is False  # off

    monkeypatch.setattr(backup, "backups_dir", lambda: tmp_path)
    for day in range(1, 6):
        folder = tmp_path / f"2026050{day}T000000Z"
        folder.mkdir()
        (folder / "manifest.json").write_text("{}")
    (tmp_path / "not-a-backup").mkdir()
    removed = backup.prune_backups(2)
    assert sorted(removed) == ["20260501T000000Z", "20260502T000000Z", "20260503T000000Z"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["20260504T000000Z", "20260505T000000Z", "not-a-backup"]
    assert backup.prune_backups(0) == ["20260504T000000Z"]  # never deletes the newest one


# ---------- search across libraries ----------
def test_search_across_libraries_interleaves_results(monkeypatch):
    from app.api import library_routes as learn_routes
    from app.main import app

    class Lib:
        def __init__(self, i, title):
            self.id, self.title, self.name, self.path = i, title, title, f"/{i}.zim"

    class Entry:
        def __init__(self, title):
            self.title = title

    class Archive:
        def __init__(self, titles):
            self.titles = titles

        def get_entry_by_path(self, path):
            return Entry(self.titles[path])

    archives = {"/a.zim": Archive({"A1": "Water A1", "A2": "Water A2", "A3": "Water A3"}),
                "/b.zim": Archive({"B1": "Water B1"})}
    monkeypatch.setattr(learn_routes.library, "ready_libraries", lambda: [Lib("a", "Wiki"), Lib("b", "Guides")])
    monkeypatch.setattr(learn_routes.zim, "open_archive", lambda p: archives[p])
    monkeypatch.setattr(learn_routes.zim, "search", lambda p, q, n: list(archives[p].titles)[:n])
    with TestClient(app) as client:
        found = client.get("/api/search/libraries", params={"q": "water", "limit": 3}).json()["results"]
    assert [(r["library"], r["path"]) for r in found] == [("Wiki", "A1"), ("Guides", "B1"), ("Wiki", "A2")]


# ---------- setup ----------
def test_setup_status_and_completion():
    from app.main import app

    with TestClient(app) as client:
        s = client.get("/api/setup").json()
        assert s["done"] is False
        ids = [x["id"] for x in s["steps"]]
        assert ids == ["runtime", "llm", "embedder", "library", "cloud"]
        assert next(x for x in s["steps"] if x["id"] == "cloud")["optional"] is True
        assert next(x for x in s["steps"] if x["id"] == "llm")["model"]
        assert client.post("/api/setup/done").json() == {"done": True}
        assert client.get("/api/setup").json()["done"] is True
        client.put("/api/settings", json={"values": {"SETUP_DONE": ""}})
