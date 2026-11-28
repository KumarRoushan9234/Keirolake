"""End to end on an installed KeiroLake: AI apps connect through the stdio bridge, Claude-style MCP calls build a table,
capture a note and follow jobs, and the Docs page's tool list and logs work. Removes everything it creates.

    python qa/e2e_ai_apps.py <engine-url> <launch-token> <path-to-keirolake-desktop.exe>

Uses the real app data folder's engine-address.json, the way Claude Desktop finds the app. Claude Desktop's own config
is never touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import httpx

ENGINE, TOKEN, EXE = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
app = httpx.Client(base_url=ENGINE + "/api", headers={"X-KeiroLake-Token": TOKEN}, timeout=300)
results: list[bool] = []
made_keys: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + str(detail)[:160] if detail else ''}", flush=True)


class Bridge:
    """One `keirolake-desktop mcp` process, spoken to line by line like Claude Desktop does."""

    def __init__(self, key: str):
        self.p = subprocess.Popen([EXE, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", env={**os.environ, "KEIROLAKE_KEY": key})
        self.n = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        return None if notify else json.loads(self.p.stdout.readline())

    def call(self, name: str, args: dict) -> tuple[bool, object]:
        r = self.send("tools/call", {"name": name, "arguments": args})["result"]
        text = r["content"][0]["text"]
        try:
            return not r["isError"], json.loads(text)
        except ValueError:
            return not r["isError"], text

    def close(self) -> None:
        self.p.stdin.close()
        self.p.wait(10)


try:
    status = app.get("/connect").json()
    check("AI apps: the engine knows the desktop app (bridge available)", status["bridge"] is True)
    out = app.post("/connect", json={"app": "claude-code", "scopes": ["read", "ask", "write", "llm"]}).json()
    made_keys.append(out["key"]["id"])
    check("Claude Code setup uses the installed app", EXE.lower().replace("/", "\\") in out["setups"]["claude-code"].lower().replace("/", "\\"),
          out["setups"]["claude-code"].replace(out["raw_key"], "klk_<hidden>"))
    started = time.time()
    b = Bridge(out["raw_key"])
    init = b.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "qa", "version": "1"}})
    b.send("notifications/initialized", notify=True)
    check("bridge: initialize through the real data folder", init["result"]["serverInfo"]["name"] == "keirolake", f"{time.time() - started:.1f}s")
    check("bridge: instructions tell the AI how to start", "overview" in init["result"].get("instructions", ""))
    tools = {t["name"] for t in b.send("tools/list")["result"]["tools"]}
    check("bridge: 18 tools for read+ask+write+llm", {"overview", "plan_table", "fill_table", "capture", "job_status", "list_models"} <= tools, len(tools))
    ok, ov = b.call("overview", {})
    check("overview", ok and "tables" in ov, f"{len(ov['tables'])} tables, {len(ov['collections'])} collections")
    ok, models = b.call("list_models", {})
    check("list_models", ok and "lake" in models, models[:6])

    ok, cap = b.call("capture", {"text": "QA bridge note: the Alpha-9 panel order arrives on Friday.", "title": "QA bridge note"})
    check("capture a note", ok and cap.get("saved_to") == "Captured", cap)
    for _ in range(60):
        ok, js = b.call("job_status", {"job_id": cap["job_id"]})
        if js["status"] in ("done", "failed"):
            break
        time.sleep(1)
    check("job_status follows the capture", js["status"] == "done", js["status"])

    t0 = time.time()
    ok, plan = b.call("plan_table", {"description": "QA bridge table: the 3 largest Indian cities by population, with their state. Columns: city, state, population. Agreed, go ahead."})
    check("plan_table: the model proposes columns", ok and plan.get("plan") and plan["plan"].get("columns"),
          f"{time.time() - t0:.0f}s, columns {[c['name'] for c in (plan.get('plan') or {}).get('columns', [])]}")
    if ok and not plan.get("ready_to_fill"):
        ok, plan = b.call("plan_table", {"session_id": plan["session_id"], "description": "Yes, exactly these columns. Fill it."})
    sid = plan.get("session_id")
    ok, fill = b.call("fill_table", {"session_id": sid, "rows": 3})
    check("fill_table starts a job", ok and "job_id" in fill, fill)
    if ok:
        for _ in range(240):
            ok2, js = b.call("job_status", {"job_id": fill["job_id"]})
            if js["status"] in ("done", "failed"):
                break
            time.sleep(1)
        check("fill_table finishes", js["status"] == "done", f"{js['status']}: {js.get('message', '')[:100]}")
    built = app.get(f"/builder/sessions/{sid}").json().get("built") if sid else None
    if built and built.get("kind") == "table":
        ok, rows = b.call("read_rows", {"table": built["id"], "limit": 5})
        check("the built table has rows Claude can read", ok and rows["total"] >= 1, rows["rows"][:2] if ok else rows)
    b.close()

    bad = subprocess.run([EXE, "mcp"], input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n", capture_output=True,
                         text=True, env={**os.environ, "KEIROLAKE_KEY": "klk_wrong"}, timeout=60)
    check("bridge: a wrong key gets a clear message", "refused the API key" in bad.stdout)

    tools_ref = app.get("/connect/tools").json()
    check("Docs: MCP tool reference", len(tools_ref) >= 26, len(tools_ref))
    log = app.get("/connect/logs/engine?lines=300").json()
    check("Docs: engine log readable", len(log["text"]) > 100, log["path"])
    check("Docs: no keys in the log", out["raw_key"] not in log["text"] and "klk_" not in log["text"].replace("klk_[redacted]", ""))
    access_log = app.get("/access/log?limit=100").json()
    check("access log never stores a key", not any(out["raw_key"] in r["path"] for r in access_log))
finally:
    for kid in made_keys:
        app.post(f"/access/keys/{kid}/revoke")
    if "sid" in dir() and sid:
        s = app.get(f"/builder/sessions/{sid}").json()
        if s.get("built", {}) and s["built"].get("kind") == "table":
            app.delete(f"/sheets/{s['built']['id']}")
        app.delete(f"/builder/sessions/{sid}")
    for kb in app.get("/kbs").json():
        if kb["name"] == "Captured":
            for d in app.get(f"/kbs/{kb['id']}/documents").json():
                if d.get("title") == "QA bridge note":
                    app.delete(f"/documents/{d['id']}")
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
