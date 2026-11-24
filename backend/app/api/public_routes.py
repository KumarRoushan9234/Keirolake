"""The data API for the user's own apps: /v1 (REST) and /mcp (Model Context Protocol).

Every request needs an API key made in the app (Authorization: Bearer klk_...).
Keys carry scopes, may be limited to some knowledge bases, are rate limited and
every call is logged. The private vault is never reachable from here.
"""
from __future__ import annotations

import csv
import io
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app import access
from app.catalog.models import Chunk, Document, KnowledgeBase
from app.db import session_scope
from app.ingest import web as ingest_web
from app.jobs.runner import runner
from app.lake import warehouse
from app.rag.retrieve import retrieve

router = APIRouter(prefix="/v1", tags=["Data API"])
mcp_router = APIRouter(tags=["MCP"])
from app.support import APP_VERSION  # noqa: E402 - one version for the whole app


def _raw_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key")


def caller(request: Request) -> dict:
    key = access.verify(_raw_key(request))
    if key is None:
        raise HTTPException(401, "Missing, wrong, revoked or expired API key", headers={"WWW-Authenticate": "Bearer"})
    if not access.rate_ok(key["id"]):
        raise HTTPException(429, f"Too many requests: at most {access.RATE_PER_MINUTE} a minute per key")
    request.state.api_key = key
    return key


def need(key: dict, scope: str) -> None:
    if scope not in key["scopes"]:
        raise HTTPException(403, f"This key doesn't have the '{scope}' permission")


def allowed_kbs(key: dict) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at))
        out = []
        for kb in rows:
            if key["kb_ids"] and kb.id not in key["kb_ids"]:
                continue
            docs = s.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb.id))
            chunks = s.scalar(select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb.id))
            out.append({"id": kb.id, "name": kb.name, "description": kb.description, "documents": docs, "chunks": chunks})
        return out


def kb_for(key: dict, kb_id: str) -> dict:
    kb = next((k for k in allowed_kbs(key) if k["id"] == kb_id or k["name"] == kb_id), None)
    if kb is None:
        raise HTTPException(404, "Knowledge base not found, or this key can't use it")
    return kb


def user_tables() -> list[dict]:
    return [t for t in warehouse.schema() if t["kind"] != "system"]


# ---------- REST ----------
@router.get("/me")
async def me(key: dict = Depends(caller)) -> dict:
    return {"name": key["name"], "scopes": key["scopes"], "kb_ids": key["kb_ids"], "expires_at": key["expires_at"]}


@router.get("/kbs")
async def kbs(key: dict = Depends(caller)) -> list[dict]:
    need(key, "read")
    return allowed_kbs(key)


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(8, ge=1, le=30)


@router.post("/kbs/{kb_id}/search")
async def search(kb_id: str, body: SearchBody, key: dict = Depends(caller)) -> dict:
    need(key, "read")
    kb = kb_for(key, kb_id)
    result = await retrieve(kb["id"], body.query, body.top_k)
    return {"hits": [{k: v for k, v in h.to_dict().items() if k in ("chunk_id", "doc_id", "title", "url", "text", "score", "cosine")}
                     for h in result.hits]}


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    kb_id: str | None = None
    web: str = Field("never", pattern="^(never|auto)$")
    libraries: bool = True


@router.post("/ask")
async def ask(body: AskBody, key: dict = Depends(caller)) -> dict:
    need(key, "ask")
    return await _answer(key, body)


async def _answer(key: dict, body: AskBody) -> dict:
    from app.rag.answer import answer_stream

    kb_id = kb_for(key, body.kb_id)["id"] if body.kb_id else None
    if not kb_id and key["kb_ids"]:
        if len(key["kb_ids"]) != 1:
            raise HTTPException(400, "This key is limited to some knowledge bases: pass kb_id")
        kb_id = key["kb_ids"][0]
    text, model, sources, error = "", "", {}, None
    async for e in answer_stream(kb_id, body.question, None, None, body.web, body.libraries, False, "", use_all_kbs=not key["kb_ids"]):
        if e["type"] == "token":
            text += e["text"]
        elif e["type"] == "meta":
            model = e["model"]
        elif e["type"] == "sources":
            sources = {k: [{"title": x.get("title"), "url": x.get("url"), "library": x.get("library")} for x in e.get(k) or []]
                       for k in ("local", "library", "web")}
        elif e["type"] == "error":
            error = e["message"]
    if error and not text:
        raise HTTPException(502, error)
    return {"answer": text.strip(), "model": model, "sources": sources}


class DocBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=2_000_000)


@router.post("/kbs/{kb_id}/documents", status_code=202)
async def add_document(kb_id: str, body: DocBody, key: dict = Depends(caller)) -> dict:
    need(key, "write")
    kb = kb_for(key, kb_id)
    job_id = runner.submit("ingest", f"API ({key['name']}): {body.title[:60]}", ingest_web.text_job(kb["id"], body.title, body.text))
    return {"job_id": job_id, "status_url": f"/v1/jobs/{job_id}"}


@router.get("/jobs/{job_id}")
async def job(job_id: str, key: dict = Depends(caller)) -> dict:
    from app.catalog.models import Job

    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None or not (j.title or "").startswith(f"API ({key['name']})"):
            raise HTTPException(404, "Job not found")
        return {"id": j.id, "status": j.status, "progress": j.progress, "message": j.message}


@router.get("/datasets")
async def datasets(key: dict = Depends(caller)) -> list[dict]:
    need(key, "read")
    return [{"table": t["table"], "rows": t["rows"], "columns": t["columns"]} for t in user_tables()]


@router.get("/datasets/{table}")
async def dataset_rows(table: str, format: str = "json", limit: int = 1000, offset: int = 0, key: dict = Depends(caller)):
    need(key, "read")
    if table not in {t["table"] for t in user_tables()} or not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise HTTPException(404, "No such dataset")
    limit, offset = max(1, min(limit, warehouse.MAX_ROWS)), max(0, offset)
    result = warehouse.run_query(f'SELECT * FROM "{table}" LIMIT {limit} OFFSET {offset}', limit, user_tables_only=True)
    return export(result, format, table)


class SQLBody(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)
    limit: int = Field(1000, ge=1, le=5000)


@router.post("/sql")
async def sql(body: SQLBody, format: str = "json", key: dict = Depends(caller)):
    need(key, "sql")
    try:
        result = warehouse.run_query(body.sql, body.limit, user_tables_only=True)
    except warehouse.SQLValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return export(result, format, "query")


def export(result: dict, format: str, name: str):
    """Rows as JSON (columns + rows), JSON Lines or CSV."""
    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(result["columns"])
        writer.writerows(result["rows"])
        return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    if format == "jsonl":
        lines = "\n".join(json.dumps(dict(zip(result["columns"], r)), default=str) for r in result["rows"])
        return Response(lines + "\n", media_type="application/x-ndjson")
    if format != "json":
        raise HTTPException(400, "format must be json, jsonl or csv")
    return {"columns": result["columns"], "rows": result["rows"], "row_count": result["row_count"], "truncated": result["truncated"]}


# ---------- sheets ----------
@router.get("/sheets")
async def sheets(key: dict = Depends(caller)) -> list[dict]:
    need(key, "read")
    from app.sheets import service as sheet_service

    return [{"id": s["id"], "name": s["name"], "table": s["table"], "rows": s["rows"],
             "columns": [{"name": c["name"], "type": c["type"]} for c in s["columns"]]} for s in sheet_service.list_sheets()]


@router.get("/sheets/{sheet_id}/rows")
async def sheet_rows(sheet_id: str, offset: int = 0, limit: int = 200, q: str = "", key: dict = Depends(caller)) -> dict:
    need(key, "read")
    from app.sheets import service as sheet_service

    try:
        out = sheet_service.rows(sheet_id, offset, limit, None, False, q)
    except KeyError:
        raise HTTPException(404, "Sheet not found") from None
    for r in out["rows"]:
        r.pop("_meta", None)
    return out


class RowsBody(BaseModel):
    rows: list[dict] = Field(min_length=1, max_length=5000)


@router.post("/sheets/{sheet_id}/rows", status_code=201)
async def sheet_add_rows(sheet_id: str, body: RowsBody, key: dict = Depends(caller)) -> dict:
    need(key, "write")
    from app.sheets import service as sheet_service

    try:
        return {"ids": sheet_service.add_rows(sheet_id, body.rows)}
    except KeyError:
        raise HTTPException(404, "Sheet not found") from None
    except sheet_service.SheetError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------- MCP (Streamable HTTP, JSON responses) ----------
TOOLS = [
    ("read", {"name": "list_knowledge_bases", "description": "List the knowledge bases this key can use.",
              "inputSchema": {"type": "object", "properties": {}}}),
    ("read", {"name": "search", "description": "Search one knowledge base by meaning and keywords. Returns passages with titles and URLs.",
              "inputSchema": {"type": "object", "required": ["kb", "query"],
                              "properties": {"kb": {"type": "string", "description": "Knowledge base id or name"},
                                             "query": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1, "maximum": 30}}}}),
    ("read", {"name": "list_datasets", "description": "List the warehouse tables with their columns and row counts.",
              "inputSchema": {"type": "object", "properties": {}}}),
    ("ask", {"name": "ask", "description": "Ask a question; get an answer with sources from the user's data and offline libraries.",
             "inputSchema": {"type": "object", "required": ["question"],
                             "properties": {"question": {"type": "string"}, "kb": {"type": "string"}}}}),
    ("sql", {"name": "query_sql", "description": "Run one read-only SQLite SELECT on the warehouse tables.",
             "inputSchema": {"type": "object", "required": ["sql"], "properties": {"sql": {"type": "string"}}}}),
    ("write", {"name": "add_document", "description": "Add a text document to a knowledge base.",
               "inputSchema": {"type": "object", "required": ["kb", "title", "text"],
                               "properties": {"kb": {"type": "string"}, "title": {"type": "string"}, "text": {"type": "string"}}}}),
    ("read", {"name": "list_tables", "description": "List the lake's tables with their columns, row counts and ids.",
              "inputSchema": {"type": "object", "properties": {}}}),
    ("read", {"name": "read_rows", "description": "Read rows from a table. Filters are 'column:op:value' (op: eq ne gt gte lt lte like in null).",
              "inputSchema": {"type": "object", "required": ["table"], "properties": {
                  "table": {"type": "string", "description": "Table id"}, "where": {"type": "array", "items": {"type": "string"}},
                  "order": {"type": "string", "description": "Column, or -column for descending"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}}}),
    ("read", {"name": "search_table", "description": "Find rows in an indexed table by meaning.",
              "inputSchema": {"type": "object", "required": ["table", "query"], "properties": {"table": {"type": "string"}, "query": {"type": "string"}}}}),
    ("write", {"name": "insert_rows", "description": "Add rows to a table. Each row is an object of column: value.",
               "inputSchema": {"type": "object", "required": ["table", "rows"], "properties": {"table": {"type": "string"}, "rows": {"type": "array", "items": {"type": "object"}}}}}),
    ("write", {"name": "update_row", "description": "Change some columns of one row.",
               "inputSchema": {"type": "object", "required": ["table", "row_id", "values"], "properties": {
                   "table": {"type": "string"}, "row_id": {"type": "integer"}, "values": {"type": "object"}}}}),
    ("read", {"name": "list_topics", "description": "List the topics held in the lake, with their table and collection ids.",
              "inputSchema": {"type": "object", "properties": {}}}),
    ("write", {"name": "build_topic", "description": "Start gathering a new topic from the web into a cleaned table and searchable pages.",
               "inputSchema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "question": {"type": "string"}}}}),
]


def _rpc(id_, result=None, error: tuple[int, str] | None = None) -> dict:
    out = {"jsonrpc": "2.0", "id": id_}
    if error:
        out["error"] = {"code": error[0], "message": error[1]}
    else:
        out["result"] = result
    return out


async def _call_tool(key: dict, name: str, args: dict) -> str:
    scope = next((s for s, t in TOOLS if t["name"] == name), None)
    if scope is None:
        raise HTTPException(404, f"Unknown tool {name}")
    need(key, scope)
    if name == "list_knowledge_bases":
        return json.dumps(allowed_kbs(key))
    if name == "list_datasets":
        return json.dumps([{"table": t["table"], "rows": t["rows"], "columns": [c["name"] for c in t["columns"]]} for t in user_tables()])
    if name == "search":
        kb = kb_for(key, str(args.get("kb", "")))
        result = await retrieve(kb["id"], str(args.get("query", "")), int(args.get("top_k") or 6))
        return json.dumps([{"title": h.title, "url": h.url, "text": h.text[:1500]} for h in result.hits])
    if name == "ask":
        out = await _answer(key, AskBody(question=str(args.get("question", "")), kb_id=args.get("kb") or None))
        return out["answer"] + "\n\nSources: " + json.dumps(out["sources"])
    if name == "query_sql":
        try:
            result = warehouse.run_query(str(args.get("sql", "")), 200, user_tables_only=True)
        except warehouse.SQLValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        return json.dumps({"columns": result["columns"], "rows": result["rows"]}, default=str)
    if name in ("list_tables", "read_rows", "search_table", "insert_rows", "update_row", "list_topics", "build_topic"):
        return await _table_tool(key, name, args)
    kb = kb_for(key, str(args.get("kb", "")))
    job_id = runner.submit("ingest", f"API ({key['name']}): {str(args.get('title'))[:60]}",
                           ingest_web.text_job(kb["id"], str(args.get("title", ""))[:300], str(args.get("text", ""))))
    return f"Queued as job {job_id}"


async def _table_tool(key: dict, name: str, args: dict) -> str:
    from app import tables_api, topics
    from app.sheets import service as sheet_service

    try:
        if name == "list_tables":
            return json.dumps([{"id": t["id"], "name": t["name"], "rows": t["rows"], "columns": [f"{c['name']} ({c['type']})" for c in t["columns"]]}
                               for t in sheet_service.list_sheets()])
        table = str(args.get("table", ""))
        if name == "read_rows":
            out = tables_api.query(table, [str(w) for w in args.get("where") or []], str(args.get("order") or ""), min(int(args.get("limit") or 50), 500))
            return json.dumps({"total": out["total"], "rows": out["rows"]}, default=str)
        if name == "search_table":
            return json.dumps(await tables_api.search(table, str(args.get("query", "")), 10), default=str)
        if name == "insert_rows":
            return json.dumps({"ids": sheet_service.add_rows(table, list(args.get("rows") or [])[:1000])})
        if name == "update_row":
            for col, val in dict(args.get("values") or {}).items():
                sheet_service.update_cell(table, int(args["row_id"]), col, val)
            return json.dumps(tables_api.rows_by_id(table, [int(args["row_id"])]), default=str)
        if name == "list_topics":
            return json.dumps([{k: t[k] for k in ("id", "name", "question", "status", "sheet_id", "kb_id")} for t in topics.list_all()])
        t = topics.create(str(args.get("name", "")), str(args.get("question") or ""))
        job_id = runner.submit("topic", f"Topic: {t['name']}", topics.build_job(t["id"]), lane="background")
        return f"Building topic {t['name']} (id {t['id']}, job {job_id})"
    except KeyError:
        raise HTTPException(404, "No such table") from None
    except (tables_api.TableError, sheet_service.SheetError, topics.TopicError) as exc:
        raise HTTPException(400, str(exc)) from exc


@mcp_router.post("/mcp")
async def mcp(request: Request, key: dict = Depends(caller)):
    try:
        msg = await request.json()
    except ValueError:
        return JSONResponse(_rpc(None, error=(-32700, "Parse error")), status_code=400)
    if isinstance(msg, list):
        return JSONResponse(_rpc(None, error=(-32600, "Batches are not supported")), status_code=400)
    method, id_, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if id_ is None:  # a notification, such as notifications/initialized
        return Response(status_code=202)
    if method == "initialize":
        return _rpc(id_, {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "keirolake", "version": APP_VERSION},
                          "instructions": "Search, ask and query the user's own KeiroLake data. Cite titles and URLs from results."})
    if method == "ping":
        return _rpc(id_, {})
    if method == "tools/list":
        return _rpc(id_, {"tools": [t for scope, t in TOOLS if scope in key["scopes"]]})
    if method == "tools/call":
        try:
            text = await _call_tool(key, str(params.get("name")), params.get("arguments") or {})
            return _rpc(id_, {"content": [{"type": "text", "text": text}], "isError": False})
        except HTTPException as exc:
            return _rpc(id_, {"content": [{"type": "text", "text": str(exc.detail)}], "isError": True})
    return _rpc(id_, error=(-32601, f"Method not found: {method}"))
