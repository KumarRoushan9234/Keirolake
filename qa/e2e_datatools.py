"""On an installed KeiroLake: open-data tools fill tables, the RenderData catalog loads, a PDF's table becomes a sheet,
and Heretic reports its environment. Removes everything it creates.

    python qa/e2e_datatools.py <engine-url> <launch-token>
"""
import sys
import time

import httpx

ENGINE, TOKEN = sys.argv[1].rstrip("/"), sys.argv[2]
app = httpx.Client(base_url=ENGINE + "/api", headers={"X-KeiroLake-Token": TOKEN}, timeout=120)
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}", flush=True)


made, sheets, kbs = [], [], []
try:
    tools = app.get("/datatools").json()
    check("data tools listed", len(tools["templates"]) >= 10)
    cat = app.get("/datatools/renderdata/catalog")
    check("RenderData live catalog", cat.status_code == 200 and len(cat.json()["scrapers"]) >= 5, f"{len(cat.json()['scrapers'])} scrapers" if cat.status_code == 200 else cat.text[:100])
    for tid, q in (("openalex", "solar panel efficiency"), ("worldbank", "SP.POP.TOTL"), ("arxiv", "retrieval augmented generation")):
        c = app.post("/datatools/use", json={"template": tid, "input": q, "name": f"QA {tid}"}).json()
        made.append(c["id"])
        for _ in range(90):
            got = next(x for x in app.get("/connectors").json() if x["id"] == c["id"])
            if got["last_run_at"]:
                break
            time.sleep(2)
        sheets.append(got["sheet_id"])
        rows = app.get(f"/sheets/{got['sheet_id']}/rows", params={"limit": 2}).json() if got["sheet_id"] else {"total": 0}
        check(f"{tid}: table filled", rows["total"] >= 10 and not got["last_result"].get("error"), f"{rows['total']} rows" if rows["total"] else str(got["last_result"]))

    # a PDF with a table, uploaded to a collection
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    with pdf.table() as t:
        for row in (("Warehouse", "Panels"), ("Nagpur", "4812"), ("Pune", "900"), ("Surat", "2300")):
            r = t.row()
            for cell in row:
                r.cell(cell)
    kb = app.post("/kbs", json={"name": "QA pdf tables"}).json()
    kbs.append(kb["id"])
    job = app.post(f"/kbs/{kb['id']}/ingest/files", files={"files": ("qa-warehouses.pdf", bytes(pdf.output()), "application/pdf")}).json()
    for _ in range(90):
        j = app.get(f"/jobs/{job['job_id']}").json()
        if j["status"] in ("done", "failed", "error"):
            break
        time.sleep(2)
    made_sheets = j.get("result", {}).get("sheets") or []
    sheets += [s["id"] for s in made_sheets]
    check("PDF table became a sheet (packaged app)", j["status"] == "done" and made_sheets and made_sheets[0]["rows"] == 3, str(made_sheets)[:120])

    h = app.get("/models/heretic").json()
    check("Heretic environment found by the installed app", h["installed"] and h["cuda"], f"{h.get('heretic')} · torch {h.get('torch')} · {h.get('gpu')}")
except Exception as exc:  # noqa: BLE001
    check("run", False, repr(exc)[:300])
finally:
    for cid in made:
        app.delete(f"/connectors/{cid}")
    for sid in sheets:
        if sid:
            app.delete(f"/sheets/{sid}")
    for k in kbs:
        app.delete(f"/kbs/{k}")
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
