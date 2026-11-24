"""Repeat the whole check N times: unit tests, frontend build, a live engine smoke test, and the browser suite.

    python qa/run_rounds.py 7

Each round starts the engine on a throwaway data folder with every cloud key blanked, so it never spends
credits or touches your real data. Exit code is non-zero if any round fails.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BACKEND, FRONTEND, QA = ROOT / "backend", ROOT / "frontend", ROOT / "qa"
PY = BACKEND / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
NPM = "npm.cmd" if os.name == "nt" else "npm"
API_PORT, UI_PORT = 8791, 4173
KEYS = ["OPENROUTER_API_KEY", "GROQCLOUD_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "KEIROLABS_SEARCH_LITE_API_KEY", "KEIROLABS_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY"]


def run(cmd, cwd, env=None, timeout=900):
    t = time.time()
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout + p.stderr), time.time() - t


def kill(proc):
    if proc and proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.terminate()


def wait_http(url, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if httpx.get(url, timeout=3).status_code < 500:
                return True
        except httpx.HTTPError:
            time.sleep(0.5)
    return False


def api_smoke(base: str) -> list[str]:
    """A few real calls against the live engine (no cloud, no credits)."""
    problems = []
    c = httpx.Client(base_url=base, timeout=30)

    def check(label, ok, detail=""):
        if not ok:
            problems.append(f"{label} {detail}".strip())

    health = c.get("/api/health").json()
    check("health", health["qdrant"]["ok"], str(health["qdrant"]))
    check("stats", set(c.get("/api/stats").json()["counts"]) >= {"knowledge_bases", "documents", "chunks"})
    check("net", "online" in c.get("/api/net").json())
    check("emergency routes are gone", c.get("/api/emergency").status_code in (404, 405))
    check("pipelines validate rejects non-Keiro", c.post("/api/pipelines/validate", json={"spec": {"name": "x", "steps": [{"type": "scrape"}]}}).status_code == 400)
    sheet = c.post("/api/sheets", json={"name": "qa sheet", "columns": [{"name": "item"}, {"name": "qty", "type": "number"}]}).json()
    c.post(f"/api/sheets/{sheet['id']}/rows", json={"rows": [{"item": "rice", "qty": 2}]})
    check("sheet rows", c.get(f"/api/sheets/{sheet['id']}/rows").json()["total"] == 1)
    check("sheet pdf export", c.get(f"/api/sheets/{sheet['id']}/export?format=pdf").content[:5] == b"%PDF-")
    check("sheet xlsx export", c.get(f"/api/sheets/{sheet['id']}/export?format=xlsx").content[:2] == b"PK")
    check("open data catalog lists but never downloads", len(c.get("/api/opendata").json()) >= 10)
    check("statusbar", c.get("/api/statusbar").json()["vectors"]["ok"] is True)
    check("auth: no password yet", c.get("/api/auth/status").json()["password_set"] is False)
    c.delete(f"/api/sheets/{sheet['id']}")
    csv = b"region,amount\nnorth,10\nsouth,20\n"
    up = c.post("/api/datasets", files={"file": ("sales.csv", csv)})
    check("dataset upload", up.status_code in (200, 201), up.text[:100])
    q = c.post("/api/warehouse/query", json={"sql": "SELECT SUM(amount) AS t FROM ds_sales"}).json()
    check("sql", q.get("rows") == [[30]], str(q)[:100])
    check("sql write blocked", c.post("/api/warehouse/query", json={"sql": "DROP TABLE ds_sales"}).status_code == 400)
    check("vault locked", c.get("/api/vault/items").status_code == 423)
    check("local hardware", c.get("/api/local/hardware").json()["hardware"]["ram_gb"] > 0)
    check("setup plan is empty by default", c.get("/api/setup/plan").json()["total_gb"] == 0)
    check("about", bool(c.get("/api/about").json().get("version")))
    diag = c.get("/api/support/diagnostics").json()
    check("diagnostics", any(d["id"] == "database" and d["status"] == "ok" for d in diag), str(diag)[:120])
    bundle = c.get("/api/support/bundle")
    check("support bundle", bundle.status_code == 200 and bundle.content[:2] == b"PK")
    kb = c.post("/api/kbs", json={"name": "qa kb"})
    check("kb create", kb.status_code == 201, kb.text[:100])
    if kb.status_code == 201:
        check("kb delete", c.delete(f"/api/kbs/{kb.json()['id']}").status_code == 204)

    # 0.4: saved conversations, intent routing, data API, MCP, plain-language Learn
    conv = c.post("/api/conversations", json={}).json()
    ids = c.post(f"/api/conversations/{conv['id']}/ask", json={"question": "hello"}).json()
    events = c.get(f"/api/conversations/messages/{ids['message_id']}/events", timeout=60).text
    check("hello is small talk (no search)", '"route": "chat"' in events, events[:150])
    saved = c.get(f"/api/conversations/{conv['id']}").json()
    check("conversation saved", [m["role"] for m in saved["messages"]] == ["user", "assistant"], str(saved)[:150])
    created = c.post("/api/access/keys", json={"name": "qa", "scopes": ["read", "sql"]}).json()
    auth = {"Authorization": f"Bearer {created['secret']}"}
    check("v1 with key", c.get("/v1/me", headers=auth).json().get("name") == "qa")
    check("v1 without key", c.get("/v1/me").status_code == 401)
    check("v1 sql reads user tables", c.post("/v1/sql", json={"sql": "select count(*) from ds_sales"}, headers=auth).json().get("rows") == [[2]])
    check("v1 sql blocks secrets", c.post("/v1/sql", json={"sql": "select * from api_keys"}, headers=auth).status_code == 400)
    tools = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=auth).json()
    check("mcp tools", any(t["name"] == "query_sql" for t in tools["result"]["tools"]), str(tools)[:120])
    check("access log", len(c.get("/api/access/log").json()) >= 3)
    check("network off by default", c.get("/api/access").json()["network"]["mode"] == "off")
    check("library find without libraries", c.post("/api/library/find", json={"q": "water"}).json()["articles"] == [])
    s = c.get("/api/settings").json()
    check("local first default", s["mode"] == "local-first" and s["zdr"]["on"], str(s.get("mode")))
    return problems


def one_round(n: int, skip_unit: bool) -> tuple[bool, list[str]]:
    log: list[str] = []
    ok = True
    if not skip_unit:
        code, out, secs = run([str(PY), "-m", "pytest", "-q", "-m", "not integration", "-p", "no:cacheprovider"], BACKEND,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        tail = [line for line in out.strip().splitlines() if "passed" in line or "failed" in line][-1:] or ["?"]
        log.append(f"unit tests   {'ok' if code == 0 else 'FAIL'}  {tail[0]}  ({secs:.0f}s)")
        ok &= code == 0
    code, out, secs = run([NPM, "run", "build"], FRONTEND)
    log.append(f"ui build     {'ok' if code == 0 else 'FAIL'}  ({secs:.0f}s)")
    if code != 0:
        log.append(out[-600:])
        return False, log

    data = Path(tempfile.mkdtemp(prefix="keirolake-qa-"))
    env = {**os.environ, **{k: "" for k in KEYS}, "DATA_DIR": str(data), "QDRANT_PATH": str(data / "q"), "KEIROLAKE_KEYRING": "0",
           "KEIROLAKE_PORT": str(API_PORT), "PYTHONIOENCODING": "utf-8", "LOCAL_AUTOSTART": "0", "OLLAMA_BASE_URL": "http://127.0.0.1:9"}
    engine = ui = None
    try:
        engine = subprocess.Popen([str(PY), str(BACKEND / "engine_main.py")], cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_http(f"http://127.0.0.1:{API_PORT}/api/net"):
            return False, log + ["engine did not start"]
        try:
            problems = api_smoke(f"http://127.0.0.1:{API_PORT}")
        except Exception as exc:  # a crash is a failed round, not the end of the run
            problems = [f"smoke crashed: {type(exc).__name__}: {exc}"]
        log.append(f"engine smoke {'ok' if not problems else 'FAIL'}  {'; '.join(problems)[:300]}")
        ok &= not problems

        ui = subprocess.Popen([NPM, "run", "preview", "--", "--port", str(UI_PORT), "--strictPort", "--host", "127.0.0.1"], cwd=FRONTEND,
                              env={**os.environ, "API_TARGET": f"http://127.0.0.1:{API_PORT}"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_http(f"http://127.0.0.1:{UI_PORT}/"):
            return False, log + ["ui server did not start"]
        code, out, secs = run(["node", "ui_smoke.mjs", f"http://127.0.0.1:{UI_PORT}"], QA, timeout=600)
        log.append(f"browser      {'ok' if code == 0 else 'FAIL'}  ({secs:.0f}s)")
        if code != 0:
            log.append(out.strip()[-1500:])
        ok &= code == 0
    finally:
        kill(ui)
        kill(engine)
        time.sleep(1)
        shutil.rmtree(data, ignore_errors=True)
    return ok, log


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1
    skip_unit = "--skip-unit" in sys.argv
    results = []
    for n in range(1, rounds + 1):
        t = time.time()
        ok, log = one_round(n, skip_unit)
        results.append(ok)
        print(f"\n=== round {n}/{rounds}: {'PASS' if ok else 'FAIL'} ({time.time() - t:.0f}s)")
        for line in log:
            print("  " + line.replace("\n", "\n  "))
        sys.stdout.flush()
    print(f"\n{sum(results)}/{rounds} rounds passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
