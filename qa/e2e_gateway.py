"""End to end on an installed KeiroLake: the OpenAI-compatible gateway with the official `openai` package, the table API,
saved views, row search, MCP tools, the open-port listener and the internet tunnel.

    python qa/e2e_gateway.py <engine-url> <launch-token> [--openai-path DIR] [--internet]

Get the engine URL and token from `node qa/installed_check.mjs <debug-port>` (the ENGINE line). It only touches what it
creates (a key, a collection, a table, a view) and puts the sharing settings and password back as they were.
"""
from __future__ import annotations

import json
import sys
import time

import httpx

args = [a for a in sys.argv[1:] if not a.startswith("--")]
ENGINE, TOKEN = args[0].rstrip("/"), args[1]
if "--openai-path" in sys.argv:
    sys.path.insert(0, sys.argv[sys.argv.index("--openai-path") + 1])
import openai  # noqa: E402

app = httpx.Client(base_url=ENGINE + "/api", headers={"X-KeiroLake-Token": TOKEN}, timeout=120)
results: list[bool] = []
TEMP_PASSWORD = "qa-temporary-password-7781"


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}", flush=True)


def wait_job(job_id: str, timeout: int = 600) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        j = app.get(f"/jobs/{job_id}").json()
        if j["status"] in ("done", "failed", "error", "cancelled"):
            return j
        time.sleep(1.5)
    return {"status": "timeout"}


before = app.get("/access").json()["network"]
had_password = app.get("/auth/status").json()["password_set"]
key_id = kb_id = table_id = None
try:
    # a listener on this computer, and a key for the test
    net = app.put("/access/network", json={"mode": "local", "port": 8443}).json()
    base = net["urls"][0]
    check("listener on this computer", net["running"], base)
    made = app.post("/access/keys", json={"name": "QA gateway", "scopes": ["llm", "read", "write", "sql"]}).json()
    key_id, secret = made["key"]["id"] if "key" in made else made["id"], made["secret"]
    v1 = httpx.Client(base_url=base + "/v1", headers={"Authorization": f"Bearer {secret}"}, timeout=180)

    # ---- the gateway, through the official openai package ----
    client = openai.OpenAI(base_url=base + "/v1", api_key=secret)
    ids = [m.id for m in client.models.list().data]
    check("openai: models.list", "auto" in ids and "lake" in ids, f"{len(ids)} models")
    t0 = time.time()
    reply = client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "Reply with exactly the word: ready"}], max_tokens=20)
    check("openai: chat completion (local first)", bool(reply.choices[0].message.content), f"{reply.model} in {time.time() - t0:.1f}s: {reply.choices[0].message.content.strip()[:40]!r}")
    chunks = list(client.chat.completions.create(model="auto", stream=True, max_tokens=30, messages=[{"role": "user", "content": "Count from one to five."}]))
    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    check("openai: streaming", len(chunks) > 2 and bool(text), f"{len(chunks)} chunks")
    emb = client.embeddings.create(model="auto", input=["solar panels", "electric scooters"])
    check("openai: embeddings", len(emb.data) == 2 and len(emb.data[0].embedding) > 100, f"{emb.model}, {len(emb.data[0].embedding)} dims")

    # grounded answer from a collection made for the test
    kb_id = app.post("/kbs", json={"name": "QA gateway notes"}).json()["id"]
    job = app.post(f"/kbs/{kb_id}/ingest/text", json={"title": "Warehouse note", "text": "The Nagpur warehouse holds 4,812 solar panels of the Alpha-9 model, counted on 3 November."}).json()
    check("collection for grounding indexed", wait_job(job["job_id"])["status"] == "done")
    grounded = client.chat.completions.create(model=f"lake:{kb_id}", messages=[{"role": "user", "content": "How many Alpha-9 panels are in the Nagpur warehouse?"}], max_tokens=120)
    raw = grounded.model_dump()
    sources = raw["choices"][0]["message"].get("sources") or []
    check("openai: grounded answer with sources", bool(sources) and "4" in grounded.choices[0].message.content,
          f"{len(sources)} source(s): {grounded.choices[0].message.content.strip()[:80]!r}")

    # ---- tables, views, row search ----
    t = v1.post("/tables", json={"name": "QA warehouses", "columns": [{"name": "city", "type": "text"}, {"name": "panels", "type": "number"}]}).json()
    table_id = t["id"]
    v1.post(f"/tables/{table_id}/rows", json={"rows": [{"city": "Nagpur", "panels": 4812}, {"city": "Pune", "panels": 900}, {"city": "Surat", "panels": 2300}]})
    rows = v1.get(f"/tables/{table_id}/rows", params={"where": "panels:gt:1000", "order": "-panels"}).json()
    check("tables: filtered read", [r["city"] for r in rows["rows"]] == ["Nagpur", "Surat"])
    view = v1.post("/views", json={"name": "qa_big_warehouses", "sql": f'SELECT city, panels FROM "{t["table"]}" WHERE panels > 1000'})
    check("views: save and read", view.status_code == 201 and len(v1.get("/views/qa_big_warehouses").json()["rows"]) == 2)
    idx = v1.post(f"/tables/{table_id}/index").json()
    check("tables: indexed for search", wait_job(idx["job_id"])["status"] == "done")
    found = v1.get(f"/tables/{table_id}/search", params={"q": "Surat"}).json()["rows"]
    check("tables: search by meaning", bool(found) and found[0]["city"] == "Surat", ", ".join(r["city"] for r in found))

    # ---- MCP ----
    rpc = lambda method, params=None: httpx.post(base + "/mcp", headers={"Authorization": f"Bearer {secret}"}, timeout=60,  # noqa: E731
                                                 json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).json()
    tools = [x["name"] for x in rpc("tools/list")["result"]["tools"]]
    check("mcp: table and topic tools listed", {"list_tables", "read_rows", "insert_rows", "update_row", "search_table", "list_topics", "build_topic"} <= set(tools))
    out = rpc("tools/call", {"name": "insert_rows", "arguments": {"table": table_id, "rows": [{"city": "Indore", "panels": 1500}]}})["result"]
    rid = json.loads(out["content"][0]["text"])["ids"][0]
    upd = rpc("tools/call", {"name": "update_row", "arguments": {"table": table_id, "row_id": rid, "values": {"panels": 1600}}})["result"]
    read = rpc("tools/call", {"name": "read_rows", "arguments": {"table": table_id, "where": ["city:eq:Indore"]}})["result"]
    check("mcp: insert, update, read rows", not out["isError"] and not upd["isError"] and '"panels": 1600' in read["content"][0]["text"])

    # ---- open port (on the LAN address, as another device would) and the tunnel ----
    if not had_password:
        app.put("/auth/password", json={"password": TEMP_PASSWORD})
    lan = app.get("/access").json()["network"]["addresses"]
    if lan:
        op = app.put("/access/network", json={"mode": "open", "port": 18443, "bind": "", "allow": [], "upnp": False}).json()
        check("open port: listener up", op["running"] and op["mode"] == "open", ", ".join(op["urls"]))
        st = httpx.get(f"https://{lan[0]}:18443/api/auth/status", verify=False, timeout=20)
        check("open port: reachable on the LAN address over HTTPS", st.status_code == 200 and st.json()["password_set"])
        check("open port: /api needs sign-in", httpx.get(f"https://{lan[0]}:18443/api/sheets", verify=False, timeout=20).status_code == 401)
        locked = app.put("/access/network", json={"mode": "open", "port": 18443, "allow": ["203.0.113.0/24"]}).json()
        refused = httpx.get(f"https://{lan[0]}:18443/api/auth/status", verify=False, timeout=20)
        check("open port: allow-list refuses other addresses", locked["running"] and refused.status_code == 403)
        app.put("/access/network", json={"mode": "open", "port": 18443, "allow": []})
        if "--internet" in sys.argv:
            pub = app.get("/access/network/public-ip")
            check("open port: public address lookup", pub.status_code == 200, f"carrier-grade NAT: {pub.json().get('cgnat')}" if pub.status_code == 200 else pub.text[:80])
            reach = app.post("/access/network/check")
            check("open port: outside check answered", reach.status_code == 200, f"reachable={reach.json().get('reachable')}" if reach.status_code == 200 else reach.text[:120])
    if "--internet" in sys.argv:
        app.put("/access/network", json={"mode": "off", "port": before["port"]})
        tun = app.post("/network/tunnel/start", timeout=300)
        url = tun.json().get("url") if tun.status_code == 200 else ""
        check("tunnel: started", bool(url), url or tun.text[:120])
        if url:
            ok = False
            for _ in range(30):
                try:
                    ok = httpx.get(url + "/api/auth/status", timeout=15).json()["network"] is True
                    break
                except Exception:  # noqa: BLE001 - a new address takes a minute to resolve
                    time.sleep(5)
            check("tunnel: reachable from the internet", ok)
        app.post("/network/tunnel/stop")
except Exception as exc:  # noqa: BLE001
    check("run", False, repr(exc)[:300])
finally:
    if table_id:
        app.delete(f"/sheets/{table_id}")
    app.delete("/views/qa_big_warehouses")
    if kb_id:
        app.delete(f"/kbs/{kb_id}")
    if key_id:
        app.delete(f"/access/keys/{key_id}")
    app.put("/access/network", json={"mode": before["mode"], "port": before["port"], "bind": "", "allow": [], "upnp": False, "cert": "", "key": ""})
    if not had_password:
        app.delete("/auth/password")
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
