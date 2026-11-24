"""The lake as a graph: where data comes from, where it lives, how it is embedded, which models touch it, who reads it.

Every node can carry `issues` (something to fix) and `actions` (calls the app can make to fix or manage it), so the
Map page is both a picture and a place to curate. The picture is read-only: it never changes anything by itself.
"""
from __future__ import annotations

import asyncio
import json
import os

from sqlalchemy import func, select

from app import access, connectors, network, usage
from app.catalog.models import Chunk, Document, KnowledgeBase, Pipeline
from app.config import get_settings, offline_mode
from app.db import session_scope
from app.embeddings.chain import get_chain
from app.keiro.client import get_keiro
from app.library import service as library
from app.local import ollama
from app.sheets import service as sheets
from app.vector.qdrant_store import get_store

# Columns, left to right. The page lays nodes out by these.
LAYERS = ["Sources", "Builds", "Lake", "Embeddings", "Models", "Access"]


def _node(id: str, layer: str, kind: str, label: str, detail: str = "", status: str = "ok", metrics: dict | None = None,
          issues: list[str] | None = None, actions: list[dict] | None = None, go: dict | None = None) -> dict:
    return {"id": id, "layer": layer, "kind": kind, "label": label, "detail": detail, "status": status, "metrics": metrics or {},
            "issues": issues or [], "actions": actions or [], "go": go}


def _act(label: str, method: str, path: str, confirm: str | None = None, danger: bool = False, body: dict | None = None) -> dict:
    return {"label": label, "method": method, "path": path, "confirm": confirm, "danger": danger, "body": body}


# ---------- the user's own layout and additions ----------
CUSTOM_KINDS = ("app", "note")


def _state_path():
    return get_settings().data_dir / "map.json"


def load_state() -> dict:
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return {"positions": raw.get("positions", {}), "hidden": raw.get("hidden", []), "links": raw.get("links", []), "custom": raw.get("custom", [])}


def save_state(state: dict) -> dict:
    """Keep only well-formed entries; the file is the user's, never trusted as code or paths."""
    def sid(v) -> str:
        v = str(v)
        if not v or len(v) > 90:
            raise ValueError("Bad id")
        return v

    clean = {
        "positions": {sid(k): {"x": float(v["x"]), "y": float(v["y"])} for k, v in state.get("positions", {}).items() if "x" in v and "y" in v},
        "hidden": [sid(h) for h in state.get("hidden", [])],
        "links": [{"from": sid(l["from"]), "to": sid(l["to"]), "label": str(l.get("label", ""))[:40]} for l in state.get("links", []) if "from" in l and "to" in l],
        "custom": [],
    }
    for c in state.get("custom", []):
        if c.get("kind") not in CUSTOM_KINDS or c.get("layer") not in LAYERS:
            raise ValueError("A custom box needs a kind (app or note) and a column")
        clean["custom"].append({"id": sid(c["id"]), "kind": c["kind"], "layer": c["layer"], "label": str(c.get("label", ""))[:60] or "Untitled",
                                "detail": str(c.get("detail", ""))[:200]})
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean), encoding="utf-8")
    tmp.replace(path)
    return clean


def _bytes(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


async def build() -> dict:
    settings = get_settings()
    nodes: list[dict] = []
    edges: list[dict] = []

    def edge(a: str, b: str, label: str = "") -> None:
        edges.append({"from": a, "to": b, "label": label})

    qdrant, local = await asyncio.gather(get_store().healthy(), ollama.status())
    keiro = get_keiro().status()
    chain = get_chain().specs

    # ---- sources ----
    nodes.append(_node("src:web", "Sources", "web", "KeiroLabs web", "Search, read and research with zero data retention" if keiro["zdr"] else "Search, read and research",
                       "ok" if keiro["configured"] else "off", {"credits": keiro["credits_remaining"]},
                       [] if keiro["configured"] else ["No KeiroLabs key set, so web gathering is off"],
                       [] if keiro["configured"] else [_act("Add key", "GO", "")], {"page": "Settings"}))
    nodes.append(_node("src:files", "Sources", "files", "Files you add", "CSV, Excel, JSON, PDF and documents", "ok", {}, [], [], {"page": "Lake"}))
    nodes.append(_node("src:open", "Sources", "open", "Open data", "Public datasets you choose to import", "ok", {}, [], [], {"page": "Lake"}))

    # ---- lake: sheets, builds ----
    sheet_list = await asyncio.to_thread(sheets.list_sheets)
    for sh in sheet_list:
        sid = f"sheet:{sh['id']}"
        issues = []
        if sh["rows"] == 0:
            issues.append("This sheet has no rows")
        b = sh.get("build")
        if b and b.get("last_result", {}).get("warnings"):
            issues.append(f"Last refresh had {len(b['last_result']['warnings'])} warning(s)")
        acts = [_act("Open", "GO", "")]
        if b:
            acts.append(_act("Refresh now", "POST", f"/sheets/{sh['id']}/refresh"))
        acts.append(_act("Export CSV", "OPEN", f"/sheets/{sh['id']}/export?format=csv"))
        acts.append(_act("Delete sheet", "DELETE", f"/sheets/{sh['id']}", f"Delete the sheet \"{sh['name']}\" and its {sh['rows']} rows? A copy is not kept.", True))
        nodes.append(_node(sid, "Lake", "sheet", sh["name"], f"{len(sh['columns'])} columns", "warn" if issues else "ok",
                           {"rows": sh["rows"], "columns": len(sh["columns"])}, issues, acts, {"page": "Lake", "params": {"sheet": sh["id"]}}))
        origin = {"build": "src:web", "dataset": "src:open", "research": "src:web"}.get(sh["source"], "src:files")
        if b:
            bid = f"build:{b['id']}"
            every = f"every {b['interval_minutes']} min" if b["interval_minutes"] else "by hand"
            nodes.append(_node(bid, "Builds", "build", f"{sh['name']} build", every, "ok" if b["enabled"] else "off",
                               {"last_run": (b.get("last_run_at") or "never")[:16]}, [], [_act("Run now", "POST", f"/sheets/{sh['id']}/refresh")],
                               {"page": "Lake", "params": {"sheet": sh["id"]}}))
            edge("src:web", bid)
            edge(bid, sid)
        else:
            edge(origin, sid)

    # ---- lake: collections and embeddings ----
    for spec in chain:
        node_id = f"emb:{spec.vector_name}"
        local_model = spec.provider == "ollama"
        issues = []
        if local_model and not local.get("running"):
            issues.append("Ollama isn't running, so this embedder can't be used")
        nodes.append(_node(node_id, "Embeddings", "embedder", spec.model, f"{spec.provider} · {spec.dim} dimensions", "warn" if issues else "ok",
                           {"dim": spec.dim}, issues, [_act("Start Ollama", "POST", "/local/ollama/start")] if issues else [], {"page": "Models"}))
        if local_model:
            edge(node_id, "model:ollama", "runs on")

    with session_scope() as s:
        kbs = []
        for kb in s.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at)):
            docs = s.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb.id))
            chunks = s.scalar(select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb.id))
            kbs.append({"id": kb.id, "name": kb.name, "collection": kb.collection_name, "docs": docs, "chunks": chunks,
                        "models": [(m.vector_name, m.points) for m in kb.model_stats]})
        pipes = list(s.scalars(select(Pipeline).where(Pipeline.kb_id.is_not(None))))
        into_kb = {p.kb_id: p.id for p in pipes}
    chain_names = {s.vector_name for s in chain}
    for kb in kbs:
        kid = f"kb:{kb['id']}"
        issues = []
        if kb["chunks"] and not kb["models"]:
            issues.append("Passages exist but none are embedded, so meaning search can't find them")
        for vname, points in kb["models"]:
            if vname not in chain_names:
                issues.append(f"Embedded with {vname}, which is no longer in the embedding chain")
            elif points < kb["chunks"]:
                issues.append(f"{kb['chunks'] - points} passages are missing from {vname}")
        nodes.append(_node(kid, "Lake", "collection", kb["name"], f"{kb['docs']} documents · {kb['chunks']} passages", "warn" if issues else "ok",
                           {"documents": kb["docs"], "passages": kb["chunks"]}, issues,
                           [_act("Re-embed", "POST", f"/kbs/{kb['id']}/reembed", "Re-embed every passage with the current embedding models? This can take a while."),
                            _act("Delete collection", "DELETE", f"/kbs/{kb['id']}", f"Delete \"{kb['name']}\" with {kb['docs']} documents? This cannot be undone.", True)],
                           {"page": "Lake"}))
        edge("src:files", kid)
        if kb["id"] in into_kb:
            edge("src:web", kid)
        for vname, points in kb["models"]:
            if vname in chain_names:
                edge(kid, f"emb:{vname}", f"{points} vectors")

    libs = await asyncio.to_thread(library.list_libraries)
    embed_label = f"{settings.wiki_embed_model}@{settings.wiki_embed_dim}"
    wiki_embedder = "model:ollama"
    for n in nodes:  # the library embedder is an Ollama model; link to its node when it is also in the embedding chain
        if n["kind"] == "embedder" and n["label"] == settings.wiki_embed_model:
            wiki_embedder = n["id"]
    for lib in libs:
        lid = f"lib:{lib['id']}"
        tier = {0: "keyword search only", 1: "partly embedded", 2: "fully embedded"}.get(lib["index_tier"], f"tier {lib['index_tier']}")
        issues = [lib["message"]] if lib["status"] == "error" and lib["message"] else []
        acts = [_act("Embed passages", "POST", f"/libraries/{lib['id']}/index")] if lib["status"] == "ready" and lib["index_tier"] < 1 else []
        nodes.append(_node(lid, "Lake", "library", lib["title"] or lib["name"], f"{lib['articles'] or 0:,} articles · {tier}", "warn" if issues else ("ok" if lib["status"] == "ready" else "busy"),
                           {"size": _bytes(lib["size_bytes"] or 0), "embedded": lib["indexed"] or 0}, issues, acts, {"page": "Lake"}))
        edge("src:open", lid)
        if lib["index_tier"] >= 1 and lib["index_model"] == embed_label:
            edge(lid, wiki_embedder, "embedded by")

    # ---- vector store ----
    qissues = [] if qdrant.get("ok") else ["The vector store isn't answering, so meaning search is off"]
    nodes.append(_node("vec:qdrant", "Embeddings", "vectors", "Qdrant vector store", f"{len(kbs)} collections", "ok" if qdrant.get("ok") else "error",
                       {"collections": len(kbs)}, qissues, [], {"page": "Settings"}))
    for kb in kbs:
        edge(f"kb:{kb['id']}", "vec:qdrant", "vectors stored in")

    # ---- models ----
    running = bool(local.get("running"))
    loaded = local.get("loaded", [])
    nodes.append(_node("model:ollama", "Models", "runtime", "Ollama", "Local models" + (" · offline mode" if offline_mode() else ""), "ok" if running else "off",
                       {"models": len([m for m in local.get("models", []) if m.get("local")]), "loaded": len(loaded)},
                       [] if running or not local.get("installed") else ["Ollama is installed but not running"],
                       [] if running else [_act("Start Ollama", "POST", "/local/ollama/start")], {"page": "Models"}))
    for m in [m for m in local.get("models", []) if m.get("local") and "embed" not in m["name"]][:6]:
        mid = f"model:{m['name']}"
        nodes.append(_node(mid, "Models", "model", m["name"], f"{m['size_gb']} GB" + (" · loaded" if m["name"] in loaded else ""), "ok", {"size_gb": m["size_gb"]}, [], [], {"page": "Models"}))
        edge(mid, "model:ollama")
    cloud = [k for k in ("OPENROUTER", "GROQCLOUD", "DEEPSEEK", "GEMINI", "ANTHROPIC", "OPENAI") if os.getenv(f"{k}_API_KEY")]
    nodes.append(_node("model:cloud", "Models", "cloud", "Cloud models", f"{len(cloud)} provider(s) · {usage.today().get('calls', 0)} calls today" if cloud else "None configured",
                       "ok" if cloud else "off", {}, [], [], {"page": "Models"}))

    # ---- access ----
    keys = [k for k in await asyncio.to_thread(access.list_keys) if not k["revoked"]]
    hooks = await asyncio.to_thread(access.list_webhooks)
    net = network.status()["mode"]
    nodes.append(_node("acc:api", "Access", "api", "Data API and MCP", f"{len(keys)} active key(s) · network {net}", "ok" if keys else "off", {"keys": len(keys)}, [],
                       [], {"page": "Connect"}))
    nodes.append(_node("acc:hooks", "Access", "webhooks", "Webhooks", f"{len(hooks)} endpoint(s)", "ok" if hooks else "off", {"endpoints": len(hooks)},
                       [f"{h['url'][:40]} last answered {h['last_status']}" for h in hooks if h["last_status"] and str(h["last_status"])[0] not in "2"],
                       [], {"page": "Connect"}))
    for n in nodes:
        if n["kind"] == "sheet" or n["kind"] == "collection":
            edge(n["id"], "acc:api")

    # ---- connectors ----
    sheet_ids = {n["id"] for n in nodes}
    for c in await asyncio.to_thread(connectors.list_all):
        cid = f"conn:{c['id']}"
        err = (c["last_result"] or {}).get("error")
        every = f"every {c['interval_minutes']} min" if c["interval_minutes"] else "by hand"
        nodes.append(_node(cid, "Sources", "connector", c["name"], f"{c['kind']} · {every}", "error" if err else ("ok" if c["enabled"] else "off"),
                           {"added": (c["last_result"] or {}).get("added", 0)} if c["last_run_at"] else {}, [err] if err else [],
                           [_act("Run now", "POST", f"/connectors/{c['id']}/run"),
                            _act("Pause" if c["enabled"] else "Resume", "PATCH", f"/connectors/{c['id']}", body={"enabled": not c["enabled"]}),
                            _act("Delete connector", "DELETE", f"/connectors/{c['id']}", f"Delete the connector \"{c['name']}\"? Its sheet and rows stay.", True)],
                           {"page": "Map"}))
        if c["sheet_id"] and f"sheet:{c['sheet_id']}" in sheet_ids:
            edge(cid, f"sheet:{c['sheet_id']}", "fills")

    # ---- the user's own boxes, links, hidden items and positions ----
    state = load_state()
    for c in state["custom"]:
        nodes.append({**_node(c["id"], c["layer"], c["kind"], c["label"], c["detail"], "ok", {}, [], [], None), "custom": True})
    ids = {n["id"] for n in nodes}
    edges = [e for e in edges if e["from"] in ids and e["to"] in ids]
    edges += [{**l, "custom": True} for l in state["links"] if l["from"] in ids and l["to"] in ids]
    hidden = set(state["hidden"])
    hidden_info = [{"id": n["id"], "label": n["label"]} for n in nodes if n["id"] in hidden]
    nodes = [n for n in nodes if n["id"] not in hidden]
    ids = {n["id"] for n in nodes}
    edges = [e for e in edges if e["from"] in ids and e["to"] in ids]
    for n in nodes:
        n["pos"] = state["positions"].get(n["id"])
    problems = sum(len(n["issues"]) for n in nodes)
    return {"layers": LAYERS, "hidden": hidden_info, "nodes": nodes, "edges": edges, "problems": problems,
            "totals": {"sheets": len(sheet_list), "collections": len(kbs), "libraries": len(libs), "rows": sum(s["rows"] for s in sheet_list)}}
