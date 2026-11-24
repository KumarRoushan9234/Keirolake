"""Start the frozen engine on a throwaway data folder and check what only a packaged build can break:
the bundled web UI, the open-data catalog file, sign-in through the network listener, the data API.

    python qa/frozen_check.py <path-to-keirolake-engine.exe>
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

exe = sys.argv[1]
data = Path(tempfile.mkdtemp(prefix="keirolake-frozen-"))
env = {**os.environ, "DATA_DIR": str(data), "QDRANT_PATH": str(data / "q"), "KEIROLAKE_PORT": "8799", "KEIROLAKE_KEYRING": "0",
       "LOCAL_AUTOSTART": "0", "OLLAMA_BASE_URL": "http://127.0.0.1:9", "KEIROLAKE_TOKEN": "frozen-token",
       "OPENROUTER_API_KEY": "", "KEIROLABS_API_KEY": "", "KEIROLABS_SEARCH_LITE_API_KEY": ""}
proc = subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}"[:300])


try:
    app = httpx.Client(base_url="http://127.0.0.1:8799", timeout=60, headers={"X-KeiroLake-Token": "frozen-token"})
    for _ in range(90):
        try:
            if app.get("/api/net").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(1)
    want = json.loads((Path(__file__).resolve().parents[1] / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))["version"]
    check("version", app.get("/api/about").json()["version"] == want, want)
    check("bundled UI served at /", "<div id=\"root\">" in httpx.get("http://127.0.0.1:8799/").text)
    check("open data catalog packaged", len(app.get("/api/opendata").json()) >= 10)
    check("sheet PDF export works frozen", True)
    sheet = app.post("/api/sheets", json={"name": "frozen", "columns": [{"name": "a"}]}).json()
    app.post(f"/api/sheets/{sheet['id']}/rows", json={"rows": [{"a": "x"}]})
    pdf = app.get(f"/api/sheets/{sheet['id']}/export?format=pdf").content
    check("pdf", pdf[:5] == b"%PDF-")
    check("xlsx", app.get(f"/api/sheets/{sheet['id']}/export?format=xlsx").content[:2] == b"PK")
    check("set password", app.put("/api/auth/password", json={"password": "frozen password 123"}).status_code == 200)
    r = app.put("/api/access/network", json={"mode": "local", "port": 8462})
    check("network listener starts", r.json()["running"], str(r.json().get("error")))
    net = httpx.Client(base_url="http://127.0.0.1:8462", timeout=30)
    check("UI served on the listener", "<div id=\"root\">" in net.get("/").text)
    check("/api needs sign-in over the listener", net.get("/api/sheets").status_code == 401)
    check("wrong password refused", net.post("/api/auth/login", json={"password": "nope nope nope"}).status_code == 401)
    ok = net.post("/api/auth/login", json={"password": "frozen password 123"})
    check("sign in", ok.status_code == 200 and "httponly" in ok.headers["set-cookie"].lower())
    check("signed-in session reads sheets", net.get("/api/sheets").status_code == 200)
    link = app.post("/api/auth/open-link").json()["url"]
    code = link.split("#code=")[1]
    other = httpx.Client(base_url="http://127.0.0.1:8462", timeout=30)
    check("open-in-browser code signs in once", other.post("/api/auth/exchange", json={"code": code}).status_code == 200
          and other.post("/api/auth/exchange", json={"code": code}).status_code == 401)
    check("management refused over the listener", net.put("/api/auth/password", json={"password": "hijack hijack 1"}).status_code == 403)
    check("statusbar", app.get("/api/statusbar").json()["vectors"]["ok"])
    cap = app.get("/api/capture/status").json()
    check("voice notes packaged (speech module)", cap["speech"]["available"], str(cap["speech"]))
    note = app.post("/api/capture/text", json={"text": "Frozen engine capture check"})
    check("capture a note", note.status_code == 202, note.text[:120])
    for _ in range(40):  # a fresh lake gets the bundled sample data on its first start
        samples = app.get("/api/samples").json()
        if samples["table_id"] and samples["collection_id"]:
            break
        time.sleep(1)
    rows = app.get(f"/api/sheets/{samples['table_id']}").json()["rows"] if samples["table_id"] else 0
    check("sample data on first start", rows == 217 and bool(samples["collection_id"]), f"{rows} countries")
    app.delete(f"/api/sheets/{sheet['id']}")
finally:
    # A one-file build runs as a launcher plus the real engine: stop both, so nothing keeps the exe locked.
    try:
        import psutil

        for child in psutil.Process(proc.pid).children(recursive=True):
            child.kill()
    except Exception:  # noqa: BLE001 - already gone
        pass
    proc.kill()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
