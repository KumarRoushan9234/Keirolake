import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app import prefs
from app.api.desktop_routes import router as desktop_router
from app.api.library_routes import router as library_router
from app.api.pipeline_routes import router as pipeline_router
from app.api.routes import router
from app.api.support_routes import router as support_router
from app.api.auth_routes import router as auth_router
from app.api.chat_routes import router as chat_router
from app.api.model_routes import router as model_router
from app.api.connector_routes import router as connector_router
from app.api.opendata_routes import router as opendata_router
from app.api.sheet_routes import router as sheet_router
from app.api.topic_routes import router as topic_router
from app.api.tunnel_routes import power_router
from app.api.tunnel_routes import router as tunnel_router
from app.api.gateway_routes import router as gateway_router
from app.api.table_routes import router as table_router
from app.api.lake_routes import router as lake_router
from app.api.builder_routes import router as builder_router
from app.api.capture_routes import router as capture_router
from app.api.datatool_routes import router as datatool_router
from app.api.public_routes import mcp_router, router as public_router
from app.api.access_routes import router as access_router
from app import network
from app import chat
from app.backup import backup_loop
from app.config import get_settings
from app.db import init_db
from app.jobs.runner import runner
from app.lake.zones import ensure_zones
from app.net import connectivity
from app.pipelines import pipeline_loop
from app.security import TokenMiddleware
from app.watch import watch_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("keirolake")

# Settings saved in the app override .env; apply them before anything reads config.
prefs.apply_saved()
prefs.reload_clients()


async def _autostart_local_model() -> None:
    """Start Ollama in the background when a local model is chosen, so offline answers are ready."""
    settings = get_settings()
    if os.getenv("LOCAL_AUTOSTART", "1") == "0" or settings.llm_mode == "cloud" or not os.getenv("LOCAL_LLM_MODEL"):
        return
    from app.local import ollama

    try:
        await ollama.start()
    except Exception as exc:  # noqa: BLE001 - the Models page shows the reason
        log.warning("Local model autostart skipped: %s", exc)


async def _warm_up() -> None:
    """Pay the cold-start costs in the background: load the offline search model, open the vector store and
    the provider connections, so the first question is as fast as the rest."""
    await asyncio.sleep(3)
    from app.library import service as library
    from app.local import ollama
    from app.vector.qdrant_store import get_store

    settings = get_settings()
    try:
        await get_store().healthy()
        if library.ready_libraries() and await ollama.has_model(settings.wiki_embed_model):
            await ollama.embed(settings.wiki_embed_model, ["warm up"], settings.wiki_embed_dim)
        from app.embeddings.chain import get_chain

        chain = get_chain()
        if chain.specs:
            await chain.embed_with(chain.specs[0], ["warm up"], "query")
    except Exception as exc:  # noqa: BLE001 - warm-up is best effort
        log.info("Warm-up skipped: %s", exc)


async def _seed_samples() -> None:
    """A fresh, empty lake gets sample data once, after the vector store is up (so the guide can be embedded)."""
    from app import samples
    from app.vector.qdrant_store import get_store

    for _ in range(30):
        try:
            if (await get_store().healthy()).get("ok"):
                break
        except Exception:  # noqa: BLE001 - still starting
            pass
        await asyncio.sleep(1)
    await samples.seed_first_run(lambda kind, title, job: runner.submit(kind, title, job, lane="background"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_zones()
    init_db()
    runner.start()
    chat.mark_interrupted()
    from app import builder_sessions

    builder_sessions.recover()
    await network.start()
    samples_task = asyncio.create_task(_seed_samples())
    from app import tunnel as _tunnel

    tunnel_task = asyncio.create_task(_tunnel.resume())
    from app import power

    power.apply_saved()
    connectivity.start()
    autostart = asyncio.create_task(_autostart_local_model())
    warm = asyncio.create_task(_warm_up())
    watcher = asyncio.create_task(watch_loop())
    backups = asyncio.create_task(backup_loop())
    pipes = asyncio.create_task(pipeline_loop())
    yield
    autostart.cancel()
    warm.cancel()
    watcher.cancel()
    backups.cancel()
    pipes.cancel()
    samples_task.cancel()
    tunnel_task.cancel()
    from app import tunnel

    from app import power as _power

    _power.stay_awake(False)
    await tunnel.stop(remember=False)  # an app restart brings the link back; only the user turns it off
    await network.stop()
    await connectivity.stop()
    from app import http

    await http.close_all()
    await runner.stop()


from app.support import APP_VERSION  # noqa: E402

app = FastAPI(title="KeiroLake", version=APP_VERSION, lifespan=lifespan,
              description="Offline-first data lake, RAG and offline libraries on Qdrant, SQLite and KeiroLabs")


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": f"Something went wrong: {type(exc).__name__}: {str(exc)[:300]}"}, status_code=500)


app.add_middleware(TokenMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origins, allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
app.include_router(desktop_router)
app.include_router(library_router)
app.include_router(pipeline_router)
app.include_router(support_router)
app.include_router(chat_router)
app.include_router(sheet_router)
app.include_router(connector_router)
app.include_router(topic_router)
app.include_router(tunnel_router)
app.include_router(power_router)
app.include_router(gateway_router)
app.include_router(table_router)
app.include_router(lake_router)
app.include_router(builder_router)
app.include_router(capture_router)
app.include_router(datatool_router)
app.include_router(model_router)
app.include_router(opendata_router)
app.include_router(public_router)
app.include_router(mcp_router)
app.include_router(access_router)
app.include_router(auth_router)


def _web_dir():
    """The built UI: bundled next to the frozen engine, or frontend/dist in a checkout."""
    import sys
    from pathlib import Path

    for base in (Path(getattr(sys, "_MEIPASS", "")) / "web" if getattr(sys, "_MEIPASS", None) else None,
                 Path(__file__).resolve().parents[2] / "frontend" / "dist"):
        if base and (base / "index.html").is_file():
            return base
    return None


if _web_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_web_dir()), html=True), name="ui")
