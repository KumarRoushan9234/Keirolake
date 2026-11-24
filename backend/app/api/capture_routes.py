"""Capture from anywhere (phone or computer): voice notes, photos, links and text, each saved by a background job.
Open to signed-in devices, like the rest of the app's own API (see app/inbox.py)."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app import inbox, speech
from app.jobs.runner import runner

router = APIRouter(prefix="/api/capture")


async def _read(f: UploadFile, max_mb: int) -> bytes:
    data = await f.read(max_mb * 2**20 + 1)
    if len(data) > max_mb * 2**20:
        raise HTTPException(413, f"That file is larger than {max_mb} MB")
    return data


@router.get("/status")
async def status() -> dict:
    """What capture can do on this computer: voice (and whether its model is downloaded) and which model reads photos."""
    return {"speech": speech.status(), "vision_model": await inbox.vision_model()}


@router.post("/audio", status_code=202)
async def audio(file: UploadFile = File(...), mode: str = Form("note")) -> dict:
    """mode=note keeps the recording and its text in "Captured"; mode=transcribe only returns the text."""
    if mode not in ("note", "transcribe"):
        raise HTTPException(400, "mode is note or transcribe")
    if not speech.available():
        raise HTTPException(501, "Voice needs the speech module, which this copy of KeiroLake doesn't include")
    data = await _read(file, inbox.MAX_AUDIO_MB)
    try:
        ext = inbox.check_upload(file.filename or "voice.webm", data, inbox.AUDIO_TYPES, inbox.MAX_AUDIO_MB)
    except inbox.CaptureError as exc:
        raise HTTPException(400, str(exc)) from exc
    if mode == "transcribe":
        return {"job_id": runner.submit("capture", "Voice to text", inbox.transcribe_job(data, ext), lane="background")}
    return {"job_id": runner.submit("capture", "Voice note", inbox.voice_note_job(data, ext), lane="background")}


@router.post("/image", status_code=202)
async def image(file: UploadFile = File(...), note: str = Form("")) -> dict:
    data = await _read(file, inbox.MAX_IMAGE_MB)
    try:
        ext = inbox.check_upload(file.filename or "photo.jpg", data, inbox.IMAGE_TYPES, inbox.MAX_IMAGE_MB)
    except inbox.CaptureError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": runner.submit("capture", "Photo", inbox.photo_job(data, ext, note[:2000]), lane="background")}


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    title: str = Field("", max_length=200)


@router.post("/text", status_code=202)
async def text(body: TextIn) -> dict:
    try:
        job, _ = await inbox.text_or_link_job(body.text, body.title)
    except Exception as exc:  # noqa: BLE001 - an unreadable or private link, or nothing to save
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": runner.submit("capture", "Link" if inbox.URL_RE.match(body.text.strip()) else "Note", job, lane="background")}


@router.post("/file", status_code=202)
async def file(files: list[UploadFile] = File(...)) -> dict:
    """Any document shared from another app (PDF, Word, a spreadsheet...), parsed and indexed into "Captured"."""
    from app.api.routes import _read_upload
    from app.ingest.web import stage_uploads

    payload = [(f.filename or "shared.bin", await _read_upload(f)) for f in files[:10]]
    if not payload or not any(data for _, data in payload):
        raise HTTPException(400, "Nothing was shared")
    return {"job_id": runner.submit("capture", f"Shared {len(payload)} file(s)", stage_uploads(await inbox.inbox_id(), payload), lane="background")}
