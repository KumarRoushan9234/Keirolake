"""Build a table or a small offline library by conversation (see app/builder.py)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import builder
from app.jobs.runner import runner
from app.llm.registry import AllModelsFailed

router = APIRouter(prefix="/api/builder")


class TurnIn(BaseModel):
    kind: str = Field(pattern="^(table|collection)$")
    messages: list[dict] = Field(min_length=1, max_length=40)
    draft: dict | None = None
    local_only: bool = False


@router.post("/turn")
async def turn(body: TurnIn) -> dict:
    try:
        return await builder.turn(body.kind, body.messages, body.draft, body.local_only)
    except AllModelsFailed as exc:
        raise HTTPException(502, "No model could answer. Choose a local model in Models.") from exc
    except builder.BuilderError as exc:
        raise HTTPException(400, str(exc)) from exc


class FillIn(BaseModel):
    kind: str = Field(pattern="^(table|collection)$")
    draft: dict
    brief: str = Field("", max_length=4000)
    count: int = Field(20, ge=1, le=builder.MAX_ROWS)
    use_lake: bool = True
    use_libraries: bool = True
    local_only: bool = False
    target_id: str | None = None  # an existing table or collection to add to


@router.post("/fill", status_code=202)
async def fill(body: FillIn) -> dict:
    draft = builder._clean_draft(body.draft, body.kind)
    if not draft["name"]:
        raise HTTPException(400, "Give it a name first")
    if body.kind == "table":
        if not draft["columns"]:
            raise HTTPException(400, "Agree on the columns first")
        job = builder.fill_table_job(draft, body.brief, body.count, body.use_lake, body.use_libraries, body.target_id, body.local_only)
    else:
        if not draft["sections"]:
            raise HTTPException(400, "Agree on the sections first")
        job = builder.fill_collection_job(draft, body.brief, body.use_lake, body.use_libraries, body.target_id, body.local_only)
    return {"job_id": runner.submit("builder", f"Build {draft['name']}", job, lane="background")}


# ---------- sessions: the conversation lives in the engine, so it keeps going when you leave the page ----------
from app import builder_sessions as sessions  # noqa: E402


def _session_error(exc: Exception) -> HTTPException:
    return HTTPException(404 if "Unknown session" in str(exc) else 409 if "Wait" in str(exc) else 400, str(exc))


class SessionIn(BaseModel):
    kind: str = Field(pattern="^(table|collection)$")
    sheet_id: str | None = None  # start from an existing table, to add rows or columns to it


@router.get("/sessions")
def list_sessions() -> list[dict]:
    return sessions.list_sessions()


@router.post("/sessions", status_code=201)
def create_session(body: SessionIn) -> dict:
    sheet = None
    if body.sheet_id:
        from app.sheets import service
        try:
            sheet = service.get(body.sheet_id)
        except Exception as exc:
            raise HTTPException(404, "That table doesn't exist") from exc
    return sessions.create(body.kind, sheet)


@router.get("/sessions/{sid}")
def get_session(sid: str) -> dict:
    try:
        return sessions.get(sid)
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc


@router.delete("/sessions/{sid}", status_code=204)
def delete_session(sid: str) -> None:
    try:
        sessions.delete(sid)
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc


class OptionsIn(BaseModel):
    lake: bool | None = None
    libraries: bool | None = None
    web: bool | None = None
    local_only: bool | None = None


@router.put("/sessions/{sid}/options")
def session_options(sid: str, body: OptionsIn) -> dict:
    try:
        return sessions.set_options(sid, body.model_dump(exclude_none=True))
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc


@router.put("/sessions/{sid}/draft")
def session_draft(sid: str, draft: dict) -> dict:
    try:
        return sessions.set_draft(sid, draft)
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc


class SayIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/sessions/{sid}/say", status_code=202)
def session_say(sid: str, body: SayIn) -> dict:
    try:
        job = sessions.say_job(sid, body.text)
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc
    return {"job_id": runner.submit("builder", "Build with AI: reply", job, lane="background")}


class SessionFillIn(BaseModel):
    count: int = Field(20, ge=1, le=builder.MAX_ROWS)


@router.post("/sessions/{sid}/fill", status_code=202)
def session_fill(sid: str, body: SessionFillIn) -> dict:
    try:
        job, s = sessions.fill_job(sid, body.count)
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc
    return {"job_id": runner.submit("builder", f"Build {s['draft']['name']}", job, lane="background")}
