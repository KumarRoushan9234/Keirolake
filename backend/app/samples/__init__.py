"""Sample data a fresh install starts with, so every page has something real to show.

- World countries: a table of 217 countries from the World Bank (CC BY 4.0), with a saved view over it.
- KeiroLake guide: a short guide as a searchable collection, so Ask can answer questions about the app.

Added once on the first start of an empty lake (SAMPLES_SEEDED marks it), and removable from Settings at any time.
Nothing is downloaded: the files ship with the app.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import select

from app import prefs
from app.catalog.models import KnowledgeBase, Sheet
from app.db import session_scope

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
TABLE = "World countries (sample)"
GUIDE = "KeiroLake guide (sample)"
VIEW = "sample_countries_by_region"
ATTRIBUTION = "World Bank World Development Indicators, CC BY 4.0. Population, GDP per person (current US$), life expectancy, CO2 per person."


def status() -> dict:
    with session_scope() as s:
        sheet = s.scalar(select(Sheet.id).where(Sheet.name == TABLE))
        kb = s.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == GUIDE))
    return {"seeded": os.getenv("SAMPLES_SEEDED") == "1", "table_id": sheet, "collection_id": kb, "present": bool(sheet or kb)}


def _empty_lake() -> bool:
    with session_scope() as s:
        return s.scalar(select(Sheet.id).limit(1)) is None and s.scalar(select(KnowledgeBase.id).limit(1)) is None


async def add(submit_job=None) -> dict:
    """Create the sample table, view and guide collection (whatever is missing)."""
    from app import tables_api
    from app.catalog import kb as kb_service
    from app.ingest import web as ingest_web
    from app.sheets import service

    st = status()
    if not st["table_id"]:
        sheet = service.import_file(TABLE, "world_countries.csv", (HERE / "world_countries.csv").read_bytes(), "Sample data: one row per country.",
                                    ATTRIBUTION, source="dataset")
        st["table_id"] = sheet["id"]
        try:
            tables_api.save_view(VIEW, f'SELECT region, COUNT(*) AS countries, SUM(population) AS population, ROUND(AVG(life_expectancy), 1) AS life_expectancy '
                                       f'FROM "{sheet["table"]}" GROUP BY region ORDER BY population DESC', "Sample: countries, people and life expectancy by region")
        except tables_api.TableError as exc:
            log.info("Sample view not saved: %s", exc)
    if not st["collection_id"]:
        try:
            kb = await kb_service.create_kb(GUIDE, "Sample data: how KeiroLake works")
            st["collection_id"] = kb.id
            job = ingest_web.text_job(kb.id, "KeiroLake guide", (HERE / "keirolake_guide.md").read_text(encoding="utf-8"))
            if submit_job:
                submit_job("ingest", "Add the sample guide", job)
        except Exception as exc:  # noqa: BLE001 - the vector store may still be starting; the table is the main sample
            log.info("Sample guide not added: %s", exc)
    prefs.update({"SAMPLES_SEEDED": "1"})
    return status()


async def remove() -> dict:
    from app import tables_api
    from app.catalog import kb as kb_service
    from app.sheets import service

    st = status()
    if st["table_id"]:
        service.delete_sheet(st["table_id"])
    tables_api.delete_view(VIEW)
    if st["collection_id"]:
        await kb_service.delete_kb(st["collection_id"])
    return status()


async def seed_first_run(submit_job=None) -> None:
    """On the very first start of an empty lake, add the samples. Never again after that, even if they are removed."""
    if os.getenv("SAMPLES_SEEDED") == "1" or os.getenv("KEIROLAKE_NO_SAMPLES") == "1" or not _empty_lake():
        if os.getenv("SAMPLES_SEEDED") != "1" and not _empty_lake():
            prefs.update({"SAMPLES_SEEDED": "1"})  # an existing lake: nothing to add
        return
    try:
        await add(submit_job)
        log.info("Sample data added")
    except Exception:  # noqa: BLE001 - samples are a nicety; never block the start
        log.exception("Couldn't add sample data")
