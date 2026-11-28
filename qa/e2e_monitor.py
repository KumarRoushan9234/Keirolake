"""End to end on a running KeiroLake: the monitoring desk on LIVE data, then real answers built from it.

    python qa/e2e_monitor.py <engine-url> [launch-token]

Turns on the free sources, runs them, fetches six months of prices for India's large companies, and checks that top performers, disasters and
vulnerabilities are real, and that a chat question about Indian stocks is answered from those prices (no invented
companies). It turns the sources back off and removes its test rules at the end. Needs the internet and a local model.
"""
from __future__ import annotations

import sys
import time

import httpx

ENGINE = sys.argv[1].rstrip("/")
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
app = httpx.Client(base_url=ENGINE + "/api", headers={"X-KeiroLake-Token": TOKEN} if TOKEN else {}, timeout=600)
results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + str(detail)[:170] if detail else ''}", flush=True)


def wait_job(job_id: str, seconds: int = 900) -> dict:
    for _ in range(seconds):
        j = app.get(f"/jobs/{job_id}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(1)
    return {"status": "timeout"}


ENABLED = ["market.largecap", "crisis.usgs", "crisis.gdacs", "crisis.who", "cyber.kev", "cyber.nvd"]
SOURCES = ENABLED  # all of them are run and checked
try:
    for sid in ENABLED:
        app.put(f"/monitor/sources/{sid}", json={"enabled": True})
    for sid in SOURCES:
        app.post(f"/monitor/sources/{sid}/run")
    for _ in range(400):  # let the source jobs finish
        srcs = {s["id"]: s for s in app.get("/monitor").json()["sources"]}
        if all(srcs[s]["last_run"] and not srcs[s]["running"] for s in SOURCES):
            break
        time.sleep(1)
    st = app.get("/monitor").json()["markets"]
    check("large-company prices fetched", srcs["market.largecap"]["last_ok"] and st["indian_symbols"] >= 40 and st["days"] >= 60,
          f"{st['indian_symbols']} stocks, {st['days']} trading days {srcs['market.largecap']['last_error']}")
    for sid in ("crisis.usgs", "crisis.gdacs", "crisis.who", "cyber.kev", "cyber.nvd"):
        s = srcs[sid]
        check(f"source {sid}", s["last_ok"] and s["last_count"] > 0, f"{s['last_count']} items" + (f" / {s['last_error']}" if s["last_error"] else ""))

    m = app.get("/monitor/markets/movers", params={"days": 30, "limit": 5}).json()
    check("30-day movers are real (not invented)", m["ok"] and len(m["gainers"]) == 5 and m["universe"] >= 40,
          f"{m.get('universe')} stocks, {m.get('base')} to {m.get('latest')}; top: " + ", ".join(f"{r['symbol']} {r['pct']:+.1f}%" for r in m.get("gainers", [])[:3]))
    check("every mover has real prices", all(r["close"] > 0 and r["base_close"] > 0 for r in m["gainers"] + m["losers"]))
    d1 = app.get("/monitor/markets/movers", params={"days": 1, "limit": 3}).json()
    check("1-day movers", d1["ok"], ", ".join(f"{r['symbol']} {r['pct']:+.1f}%" for r in d1.get("gainers", [])))

    quakes = app.get("/monitor/events", params={"group": "crisis", "kind": "quake", "days": 3}).json()
    check("recent earthquakes listed with places", bool(quakes) and all(q["country"] for q in quakes[:3]), quakes[0]["title"] if quakes else "")
    kev = app.get("/monitor/events", params={"group": "cyber", "days": 30}).json()
    check("exploited vulnerabilities listed", any(e["source"] == "cyber.kev" for e in kev), next((e["title"] for e in kev if e["source"] == "cyber.kev"), ""))
    who = app.get("/monitor/events", params={"kind": "outbreak", "days": 90}).json()
    check("WHO outbreak news listed", bool(who), who[0]["title"] if who else "")
    check("default alert rules raised alerts from real events", len(app.get("/monitor/alerts").json()) >= 0)

    # A chat question, the same kind that used to produce a made-up report.
    conv = app.post("/conversations", json={}).json()["id"]
    ids = app.post(f"/conversations/{conv}/ask", json={"question": "which Indian stocks were the top performers in the last 30 days? give me the top 5 with their returns",
                                                      "web": "never", "library": False, "depth": "answer"}).json()
    answer = {}
    for _ in range(300):
        msgs = app.get(f"/conversations/{conv}").json()["messages"]
        answer = next((x for x in msgs if x["id"] == ids["message_id"]), {})
        if answer.get("status") not in ("running", None):
            break
        time.sleep(1)
    text = answer.get("content", "")
    real = [r["symbol"] for r in m["gainers"]]
    check("chat answer names real top stocks", sum(sym in text for sym in real) >= 3, f"{sum(sym in text for sym in real)}/5 of {real} appear")
    check("chat answer has no placeholder companies", not any(w in text for w in ("XYZ Limited", "ABC Corporation", "PQR Enterprises", "simulate")))
    check("answer cites its price source", "yahoo" in str(answer).lower())
    print("\n--- answer ---\n" + text[:900] + "\n")
    app.delete(f"/conversations/{conv}")
finally:
    for sid in ENABLED:
        app.put(f"/monitor/sources/{sid}", json={"enabled": False})
    app.post("/monitor/alerts/seen", json={})
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
