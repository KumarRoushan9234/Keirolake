"""Portable runtime install, setup planning and the one-button setup job."""
import io
import tarfile
import zipfile
from pathlib import Path

import pytest
import zstandard

from app import autosetup
from app.local import install, ollama


# ---------- archive safety ----------
def _zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def test_zip_extracts_normally_and_blocks_path_traversal(tmp_path):
    ok = _zip(tmp_path / "ok.zip", {"ollama.exe": b"MZ", "lib/runner.dll": b"x"})
    install.extract(ok, tmp_path / "out")
    assert (tmp_path / "out" / "ollama.exe").read_bytes() == b"MZ" and (tmp_path / "out" / "lib" / "runner.dll").exists()
    evil = _zip(tmp_path / "evil.zip", {"../escaped.txt": b"pwned"})
    with pytest.raises(install.InstallError, match="Unsafe path"):
        install.extract(evil, tmp_path / "out2")
    assert not (tmp_path / "escaped.txt").exists()


def _tar_zst(path: Path, entries: list[tuple[str, bytes, str | None]]) -> Path:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data, link in entries:
            info = tarfile.TarInfo(name)
            if link:
                info.type, info.linkname = tarfile.SYMTYPE, link
            else:
                info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if not link else None)
    path.write_bytes(zstandard.ZstdCompressor().compress(raw.getvalue()))
    return path


def test_tar_zst_extracts_and_blocks_traversal_and_escaping_links(tmp_path):
    ok = _tar_zst(tmp_path / "ok.tar.zst", [("bin/ollama", b"\x7fELF", None), ("lib/ollama/libx.so", b"so", None)])
    install.extract(ok, tmp_path / "out")
    assert (tmp_path / "out" / "bin" / "ollama").read_bytes() == b"\x7fELF"
    with pytest.raises(install.InstallError, match="Unsafe path"):
        install.extract(_tar_zst(tmp_path / "t1.tar.zst", [("../../etc/evil", b"x", None)]), tmp_path / "o1")
    with pytest.raises(install.InstallError, match="Unsafe link"):
        install.extract(_tar_zst(tmp_path / "t2.tar.zst", [("bin/link", b"", "../../../etc/passwd")]), tmp_path / "o2")
    with pytest.raises(install.InstallError, match="Unknown archive"):
        install.extract(tmp_path / "x.rar", tmp_path / "o3")


def test_asset_name_by_platform(monkeypatch):
    for plat, machine, expected in [("win32", "AMD64", "ollama-windows-amd64.zip"), ("linux", "x86_64", "ollama-linux-amd64.tar.zst"),
                                    ("win32", "ARM64", "ollama-windows-arm64.zip")]:
        monkeypatch.setattr(install.sys, "platform", plat)
        monkeypatch.setattr(install.platform, "machine", lambda m=machine: m)
        assert install.asset_name() == expected
    monkeypatch.setattr(install.sys, "platform", "darwin")
    with pytest.raises(install.InstallError, match="ollama.com/download"):
        install.asset_name()


async def test_install_job_downloads_verifies_unpacks_and_starts(tmp_path, monkeypatch):
    import hashlib

    archive = _zip(tmp_path / "src.zip", {"ollama.exe": b"MZ-fake"})
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    started = []

    async def fake_fetch(url, target, ctx, expected):
        assert url.endswith("ollama-windows-amd64.zip") and expected == sha
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read_bytes())

    async def fake_sha(asset):
        return sha

    async def fake_start(**_):
        started.append(True)

    class Ctx:
        async def progress(self, *_a):
            pass

    monkeypatch.setattr(install, "asset_name", lambda: "ollama-windows-amd64.zip")
    monkeypatch.setattr(install, "fetch_resumable", fake_fetch)
    monkeypatch.setattr(install, "expected_sha256", fake_sha)
    monkeypatch.setattr(install.ollama, "find_exe", lambda: None)
    monkeypatch.setattr(install.ollama, "start", fake_start)
    monkeypatch.setattr(install.ollama, "portable_dir", lambda: tmp_path / "portable")
    monkeypatch.setattr(install.ollama, "portable_exe", lambda: tmp_path / "portable" / "ollama.exe")
    monkeypatch.setattr(install.sys, "platform", "win32")
    result = await install.install_job()(Ctx())
    assert result["verified"] is True and (tmp_path / "portable" / "ollama.exe").read_bytes() == b"MZ-fake" and started


def test_find_exe_uses_the_portable_copy_and_it_gets_its_own_model_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama.shutil, "which", lambda _n: None)
    exe = tmp_path / "bin" / "ollama" / ("ollama.exe" if ollama.sys.platform == "win32" else "bin/ollama")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    monkeypatch.setattr(ollama, "portable_exe", lambda: exe)
    monkeypatch.setattr(ollama, "LOCALAPPDATA_UNUSED", None, raising=False)
    assert ollama.find_exe() == str(exe)
    assert ollama.is_portable(str(exe)) is True and ollama.is_portable("/usr/bin/ollama") is False


# ---------- planning ----------
def _entry(name, gb):
    return {"name": name, "title": name, "size_bytes": int(gb * autosetup.GB), "url": f"https://x/{name}.zim", "issued": "2026-01-01"}


@pytest.fixture()
def machine(monkeypatch):
    hw = {"ram_gb": 23.0, "vram_gb": 8.0, "threads": 32, "disk_free_gb": 200.0, "cpu": "x", "cores": 16, "os": "t", "ram_free_gb": 10,
          "gpus": [], "data_dir": "d", "disk_total_gb": 500}
    state = {"installed": True, "models": []}

    async def status():
        return {"installed": state["installed"], "running": True, "models": state["models"], "exe": "x", "version": "0"}

    async def load():
        return [_entry("zimgit-water_en", 0.02), _entry("ifixit_en_all", 3.3), _entry("mdwiki_en_all_maxi", 2.1),
                _entry("wikipedia_en_all_mini", 13.4), _entry("wikipedia_en_all_nopic", 49.1), _entry("wikipedia_en_all_maxi", 118.7)], {}

    monkeypatch.setattr(autosetup.hardware, "probe", lambda: hw)
    monkeypatch.setattr(autosetup.ollama, "status", status)
    monkeypatch.setattr(autosetup.lib_catalog, "load", load)
    monkeypatch.setattr(autosetup.library, "list_libraries", lambda: [])
    return hw, state


async def test_plan_lists_only_what_is_missing_and_totals_it(machine):
    hw, state = machine
    nothing = await autosetup.make_plan()
    assert nothing.total_gb == 0 and not nothing.llm and not nothing.libraries  # nothing by default
    plan = await autosetup.make_plan(wikipedia="mini", essentials=True, local_ai=True)
    assert plan.runtime is False and plan.llm == "qwen3.5:9b" and plan.embedder == "qwen3-embedding:0.6b"
    assert {e["name"] for e in plan.libraries} == {"zimgit-water_en", "ifixit_en_all", "mdwiki_en_all_maxi"}
    assert plan.wikipedia["name"] == "wikipedia_en_all_mini" and 25 < plan.total_gb < 30

    state["models"] = [{"name": "qwen3.5:9b", "local": True}, {"name": "qwen3-embedding:0.6b", "local": True}]
    state["installed"] = False
    plan = await autosetup.make_plan(local_ai=True)
    assert plan.runtime is True and plan.llm is None and plan.embedder is None and plan.libraries == [] and plan.total_gb == autosetup.RUNTIME_GB


async def test_plan_skips_what_does_not_fit_the_disk(machine):
    hw, _ = machine
    hw["disk_free_gb"] = 40.0
    plan = await autosetup.make_plan(wikipedia="maxi")
    assert plan.wikipedia is None and any("118" in s or "119" in s for s in plan.skipped)
    hw["disk_free_gb"] = 12.0
    plan = await autosetup.make_plan(wikipedia="nopic")
    assert plan.wikipedia is None and plan.skipped


# ---------- the job ----------
class Ctx:
    def __init__(self):
        self.messages = []

    async def progress(self, fraction, message=""):
        self.messages.append((fraction, message))


async def test_auto_job_runs_stages_in_order_and_continues_past_a_failed_download(monkeypatch):
    calls = []

    async def fake_sub(kind, title, job, ctx, label, lane="background", resume=None):
        calls.append((kind, title))
        return "failed: no space" if "ifixit" in title else "done"

    started, prefs_set = [], {}

    async def start(**_):
        started.append(True)

    class FakeInstall:
        @staticmethod
        def install_job():
            return None

    monkeypatch.setattr(autosetup, "run_sub", fake_sub)
    monkeypatch.setattr(autosetup.ollama, "start", start)
    monkeypatch.setattr(autosetup.ollama, "pull_job", lambda m: None)
    monkeypatch.setattr(autosetup.install, "install_job", FakeInstall.install_job)
    monkeypatch.setattr(autosetup.library, "start_download", lambda e: ("id", None))
    monkeypatch.setattr(autosetup.prefs, "update", lambda changes: prefs_set.update(changes))
    submitted = []
    monkeypatch.setattr(autosetup.runner, "submit", lambda kind, title, fn, lane="main", resume=None: submitted.append((title, resume["type"])) or "wiki-job")

    plan = autosetup.Plan(runtime=True, llm="qwen3.5:9b", embedder="qwen3-embedding:0.6b",
                          libraries=[_entry("zimgit-water_en", 0.02), _entry("ifixit_en_all", 3.3)], wikipedia=_entry("wikipedia_en_all_mini", 13.4))
    result = await autosetup.auto_job(plan)(Ctx())
    kinds = [k for k, _ in calls]
    assert kinds[0] == "setup" and kinds.index("model") < kinds.index("library") and "emergency" not in kinds
    assert result["stages"]["runtime"] == "done" and result["stages"]["models"] == "done"
    assert "no space" in result["stages"]["libraries"] and result["stages"]["wikipedia"].startswith("downloading")
    assert "guides" not in result["stages"]
    assert prefs_set["LOCAL_LLM_MODEL"] == "qwen3.5:9b"
    assert prefs_set["SETUP_DONE"] == "1" and submitted == [("Download wikipedia_en_all_mini", "library")]


async def test_auto_job_fails_clearly_when_nothing_worked(monkeypatch):
    async def fake_sub(*a, **k):
        return "failed: offline"

    monkeypatch.setattr(autosetup, "run_sub", fake_sub)
    monkeypatch.setattr(autosetup.install, "install_job", lambda: None)
    monkeypatch.setattr(autosetup.prefs, "update", lambda c: None)
    with pytest.raises(RuntimeError, match="Setup couldn't complete"):
        await autosetup.auto_job(autosetup.Plan(runtime=True))(Ctx())


def test_message_only_progress_does_not_reset_the_bar():
    import asyncio

    from app.catalog.models import Job
    from app.db import session_scope
    from app.jobs.runner import JobContext

    with session_scope() as s:
        job = Job(kind="t", title="t", progress=0.6)
        s.add(job)
        s.flush()
        job_id = job.id
    asyncio.run(JobContext(job_id).progress(-1, "still going"))
    with session_scope() as s:
        j = s.get(Job, job_id)
        assert j.progress == 0.6 and j.message == "still going"


def test_setup_endpoints(machine):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        plan = client.get("/api/setup/plan", params={"wikipedia": "mini"}).json()
        assert plan["wikipedia"]["name"] == "wikipedia_en_all_mini" and plan["total_gb"] > 0
        assert client.get("/api/setup/plan", params={"wikipedia": "huge"}).status_code == 400
        assert client.post("/api/setup/auto", json={"wikipedia": "giant"}).status_code == 422


# ---------- jobs survive restarts ----------
def test_interrupted_jobs_with_resume_info_are_requeued(monkeypatch):
    import asyncio

    from app.catalog.models import Job
    from app.db import session_scope
    from app.jobs import resume as resumer
    from app.jobs.runner import JobRunner

    built = []
    async def slow(ctx):
        await asyncio.sleep(30)

    monkeypatch.setattr(resumer, "build", lambda spec, at: built.append(spec) or ("pipeline", "Run: x", slow, "background"))
    with session_scope() as s:
        s.add(Job(kind="pipeline", title="Run: x", status="running", result={"_resume": {"type": "pipeline", "id": "p1"}}))
        s.add(Job(kind="ingest", title="plain", status="running", result={}))
        s.add(Job(kind="pipeline", title="Run: y", status="queued", result={"_resume": {"type": "pipeline", "id": "p2", "tries": 3}}))
        s.flush()

    async def go():
        r = JobRunner()
        r.start()
        await asyncio.sleep(0)
        await r.stop()
        return r

    asyncio.run(go())
    assert [b["id"] for b in built] == ["p1"] and built[0]["tries"] == 1  # given up after 3 tries
    with session_scope() as s:
        from sqlalchemy import select

        msgs = {j.title: j.message for j in s.scalars(select(Job).where(Job.title.in_(["plain", "Run: y"])))}
        resumed = list(s.scalars(select(Job).where(Job.title == "Run: x", Job.status.in_(["queued", "running"]))))
    assert msgs["plain"] == "Interrupted by server restart" and msgs["Run: y"] == "Interrupted by server restart"
    assert resumed and resumed[0].result["_resume"]["id"] == "p1"


def test_resume_builders(monkeypatch):
    from datetime import datetime, timezone

    from app.jobs import resume as resumer

    assert resumer.build({"type": "unknown"}) is None
    assert resumer.build({"type": "pipeline", "id": "missing"}) is None
    kind, title, fn, lane = resumer.build({"type": "model", "model": "qwen3.5:4b"})
    assert (kind, title, lane) == ("model", "Download qwen3.5:4b", "background")
    assert resumer.build({"type": "emergency", "topics": ["burns"]}) is None  # guides were removed in 0.5
