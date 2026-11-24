"""Open dataset catalog: nothing downloads on its own, imports keep licence and credit, recipes convert correctly."""
import io
import zipfile

import httpx
import pytest
import respx

from app.opendata import service
from app.sheets import service as sheets

pytestmark = pytest.mark.asyncio


class Ctx:
    async def progress(self, *a):
        pass


def test_catalog_entries_are_complete_and_licensed():
    ids = set()
    for e in service.catalog():
        assert e["id"] not in ids
        ids.add(e["id"])
        for key in ("title", "publisher", "description", "url", "license", "attribution", "format", "size_mb", "category"):
            assert e.get(key), (e["id"], key)
        assert e["url"].startswith("https://") and e["format"] in ("csv", "zip-tsv")
    listing = service.listing()
    assert all("url" not in e for e in listing) and all(e["added"] is False for e in listing)  # listing never fetches or exposes URLs


def test_geonames_zip_gets_its_column_names():
    e = service.entry("geonames-cities15000")
    row = "\t".join(["1", "Pune", "Pune", "", "18.5", "73.8", "P", "PPLA2", "IN", "", "16", "", "", "", "3124458", "", "560", "Asia/Kolkata", "2024-01-01"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cities15000.txt", row + "\n")
    csv = service.to_csv_bytes(e, buf.getvalue()).decode()
    assert csv.splitlines()[0].startswith("geonameid,name,asciiname") and "Pune" in csv and "3124458" in csv


@respx.mock
async def test_import_keeps_attribution_and_marks_it_added():
    e = service.entry("language-codes")
    respx.get(e["url"]).mock(return_value=httpx.Response(200, content=b"alpha2,English\nen,English\nhi,Hindi\n"))
    out = await service.import_job("language-codes")(Ctx())
    assert out["rows"] == 2 and out["license"].startswith("ODC")
    sheet = sheets.get(out["sheet_id"])
    assert sheet["source"] == "dataset" and "datahub.io" in sheet["attribution"]
    assert next(x for x in service.listing() if x["id"] == "language-codes")["added"] is True
    body, _, _ = sheets.export(out["sheet_id"], "pdf")
    assert body[:5] == b"%PDF-"
    sheets.delete_sheet(out["sheet_id"])


@respx.mock
async def test_failed_download_is_reported():
    e = service.entry("currency-codes")
    respx.get(e["url"]).mock(return_value=httpx.Response(503))
    with pytest.raises(service.OpenDataError, match="503"):
        await service.import_job("currency-codes")(Ctx())
