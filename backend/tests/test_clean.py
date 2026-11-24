"""Cleaning a sheet: counted first, applied with a copy kept, never inventing values."""
import pytest

from app.sheets import clean, service


@pytest.fixture
def messy():
    sh = service.create_blank("messy", [{"name": "city", "type": "text"}, {"name": "price", "type": "text"}, {"name": "when", "type": "text"}, {"name": "note", "type": "text"}])
    service.add_rows(sh["id"], [
        {"city": "  Pune ", "price": "$1,200", "when": "12 Jan 2026", "note": "N/A"},
        {"city": "Pune", "price": "1200", "when": "2026-01-12", "note": "-"},
        {"city": "Delhi  City", "price": "(300)", "when": "3 Feb 2026", "note": ""},
        {"city": "", "price": "", "when": "", "note": ""},
        {"city": "Goa", "price": "2.5k", "when": "March 1, 2026", "note": None},
    ])
    yield sh["id"]
    service.delete_sheet(sh["id"])


def test_plan_counts_without_changing(messy):
    before = service.rows(messy)["rows"]
    rep = clean.plan(messy)
    assert rep["empty_rows_removed"] == 1 and rep["duplicates_removed"] == 1 and rep["empty_columns"] == ["note"]
    assert {"name": "price", "to": "number"} in rep["retyped"] and {"name": "when", "to": "date"} in rep["retyped"]
    assert service.rows(messy)["rows"] == before and not service.history(messy)


def test_apply_cleans_keeps_a_copy_and_retypes(messy):
    rep = clean.apply(messy, drop_empty_columns=True)
    info = service.get(messy)
    rows = service.rows(messy)["rows"]
    assert rep["rows_before"] == 5 and rep["rows_after"] == 3 and info["rows"] == 3
    assert [r["city"] for r in rows] == ["Pune", "Delhi City", "Goa"]
    assert [r["price"] for r in rows] == [1200.0, -300.0, 2500.0] and [r["when"] for r in rows] == ["2026-01-12", "2026-02-03", "2026-03-01"]
    types = {c["name"]: c["type"] for c in info["columns"]}
    assert types["price"] == "number" and types["when"] == "date" and "note" not in types and rep["dropped_columns"] == ["note"]
    assert service.history(messy)  # the sheet as it was before


def test_mixed_columns_are_left_alone():
    sh = service.create_blank("mixed", [{"name": "code", "type": "text"}])
    try:
        service.add_rows(sh["id"], [{"code": "A12"}, {"code": "7"}, {"code": "B9"}])
        assert clean.plan(sh["id"])["retyped"] == []
    finally:
        service.delete_sheet(sh["id"])


def test_onboarding_flag_round_trips():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.post("/api/onboarding/reset")
        assert c.get("/api/onboarding").json() == {"done": False}
        c.post("/api/onboarding/done")
        assert c.get("/api/onboarding").json() == {"done": True}
        assert c.post("/api/onboarding/nope").status_code == 404
        c.post("/api/onboarding/reset")


def test_uploaded_table_files_also_become_clean_sheets():
    from app.ingest import web

    made = web._sheet_from_file("prices.csv", b"city,price\n Pune ,$1,200\nPune,1200\n,\n".replace(b"$1,200", b'"$1,200"'))
    try:
        assert made and made["rows"] == 1
        assert web._sheet_from_file("broken.csv", b"") is None
    finally:
        if made:
            service.delete_sheet(made["id"])


def test_tables_inside_a_pdf_become_sheets():
    from fpdf import FPDF

    from app.ingest import web

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    with pdf.table() as t:
        for row in (("City", "Population"), ("Pune", "7,000,000"), ("Delhi", "30,000,000"), ("Goa", "1,500,000")):
            r = t.row()
            for cell in row:
                r.cell(cell)
    made = web._sheets_from_pdf("cities.pdf", bytes(pdf.output()))
    try:
        assert len(made) == 1 and made[0]["rows"] == 3
        cols = {c["name"]: c["type"] for c in service.get(made[0]["id"])["columns"]}
        assert cols.get("population") == "number" or cols.get("Population") == "number"
    finally:
        for m in made:
            service.delete_sheet(m["id"])
    assert web._sheets_from_pdf("bad.pdf", b"not a pdf") == []
