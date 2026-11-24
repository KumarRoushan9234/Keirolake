"""Call every open-data template for real and check it turns into rows (no key needed). Also reads RenderData's live catalog.

    python qa/live_datatools.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import connectors, datatools  # noqa: E402


async def main() -> int:
    failed = 0
    for t in datatools.TEMPLATES:
        spec = datatools.template_url(t["id"], t["example"])
        try:
            data = await connectors._get(spec["url"])
            rows = connectors.rows_from_json(data, spec["json_path"]) if spec["kind"] == "rest" else connectors.rows_from_feed(data)
            keyed = not spec["key_column"] or spec["key_column"] in rows[0]
            ok = len(rows) >= 3 and keyed
            print(f"{'PASS' if ok else 'FAIL'}  {t['name']:<22} {len(rows):>4} rows, {len(rows[0])} columns{'' if keyed else ', key column missing: ' + spec['key_column']}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"FAIL  {t['name']:<22} {str(exc)[:120]}")
        failed += not ok
    try:
        cat = await datatools.rd_catalog(force=True)
        print(f"PASS  RenderData catalog      {len(cat['scrapers'])} scrapers: {', '.join(s['id'] for s in cat['scrapers'])}")
    except Exception as exc:  # noqa: BLE001
        failed += 1
        print(f"FAIL  RenderData catalog      {exc}")
    print(f"\n{failed} failed")
    return failed


sys.exit(asyncio.run(main()))
