"""Capture: put anything into the lake from a phone in one step (a voice note, a photo of a page, a link or some text).

Everything lands in a collection called "Captured" (made on first use) and is searchable like any other document.
Each capture is a background job, so the phone can lock or go offline while the computer does the work. Voice is
transcribed on this computer (app/speech.py); photos are read by a local vision model when one is installed.
"""
from __future__ import annotations

import base64
import re
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import select

from app import speech
from app.catalog.models import KnowledgeBase
from app.db import session_scope
from app.ingest.pipeline import IncomingDoc, start_run
from app.ingest.web import _run, text_job
from app.lake.zones import raw_run_dir, write_raw_bytes
from app.local import ollama

INBOX = "Captured"
AUDIO_TYPES = (".webm", ".ogg", ".oga", ".m4a", ".mp4", ".mp3", ".wav", ".aac", ".3gp", ".flac")
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif")
MAX_AUDIO_MB = 40
MAX_IMAGE_MB = 20
URL_RE = re.compile(r"^https?://\S+$")
VISION_HINTS = ("vl", "llava", "vision", "moondream", "minicpm-v", "gemma3", "llama4")


class CaptureError(ValueError):
    pass


async def inbox_id() -> str:
    """The "Captured" collection, made the first time something is captured."""
    with session_scope() as s:
        kb = s.scalar(select(KnowledgeBase).where(KnowledgeBase.name == INBOX))
        if kb:
            return kb.id
    from app.catalog.kb import create_kb

    return (await create_kb(INBOX, "Voice notes, photos, links and text sent from your phone or the Capture page")).id


def _stamp() -> str:
    return datetime.now().strftime("%d %b %Y %H:%M")


def _title(prefix: str, text: str) -> str:
    words = re.sub(r"\s+", " ", text).strip()
    return f"{prefix}: {words[:60]}{'…' if len(words) > 60 else ''}" if words else f"{prefix} {_stamp()}"


def check_upload(name: str, data: bytes, kinds: tuple[str, ...], max_mb: int) -> str:
    ext = Path(name or "").suffix.lower() or kinds[0]
    if ext not in kinds:
        raise CaptureError(f"That file type isn't supported here ({ext})")
    if len(data) > max_mb * 2**20:
        raise CaptureError(f"That file is larger than {max_mb} MB")
    if not data:
        raise CaptureError("The file is empty")
    return ext


# ---------- voice ----------
def transcribe_job(data: bytes, ext: str):
    """Only the text (for the microphone buttons). The recording is deleted as soon as it's read."""
    async def job(ctx) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"voice{ext}"
            path.write_bytes(data)
            return await speech.transcribe(path, progress=ctx.progress)

    return job


def voice_note_job(data: bytes, ext: str):
    """Keep the recording in the lake and its text as a searchable note."""
    async def job(ctx) -> dict:
        kb_id = await inbox_id()
        source_id, run_id = start_run("voice", f"Voice note {_stamp()}", kb_id)
        raw_dir = raw_run_dir("voice", run_id)
        audio = write_raw_bytes(raw_dir, f"recording{ext}", data)
        out = await speech.transcribe(audio, progress=ctx.progress)
        if not out["text"]:
            raise CaptureError("No speech was heard in the recording")
        await ctx.progress(0.8, "Saving the note")
        doc = IncomingDoc(_title("Voice note", out["text"]), out["text"], audio, "text/markdown", None,
                          {"captured": "voice", "language": out["language"], "seconds": out["seconds"]})
        result = await _run(kb_id, source_id, run_id, raw_dir, [doc], ctx)
        return {**result, "kb_id": kb_id, "text": out["text"]}

    return job


# ---------- photos ----------
async def vision_model() -> str | None:
    """A model on this computer that can read images, if there is one (Ollama's cloud models are skipped)."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            tags = (await c.get(f"{ollama.base_url()}/api/tags")).json().get("models", [])
            for m in tags:
                name = m.get("name", "")
                if m.get("remote_host") or name.endswith("cloud"):  # Ollama's cloud models run elsewhere: never send photos there
                    continue
                show = (await c.post(f"{ollama.base_url()}/api/show", json={"model": name})).json()
                if "vision" in (show.get("capabilities") or []) or any(h in name.lower() for h in VISION_HINTS):
                    return name
    except (httpx.HTTPError, ValueError):
        return None
    return None


READ_PROMPT = ("Write out all the text in this image exactly, keeping its structure as Markdown. "
               "If there is little or no text, describe what the image shows in two or three sentences.")


async def read_image(model: str, data: bytes) -> str:
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(f"{ollama.base_url()}/api/chat", json={"model": model, "stream": False, "messages": [
            {"role": "user", "content": READ_PROMPT, "images": [base64.b64encode(data).decode()]}]})
        r.raise_for_status()
        return (r.json().get("message") or {}).get("content", "").strip()


def photo_job(data: bytes, ext: str, note: str = ""):
    async def job(ctx) -> dict:
        kb_id = await inbox_id()
        source_id, run_id = start_run("photo", f"Photo {_stamp()}", kb_id)
        raw_dir = raw_run_dir("photo", run_id)
        image = write_raw_bytes(raw_dir, f"photo{ext}", data)
        model = await vision_model()
        text = ""
        if model:
            await ctx.progress(0.3, f"Reading the photo with {model}")
            try:
                text = await read_image(model, data)
            except httpx.HTTPError:
                text = ""
        body = "\n\n".join(x for x in (note.strip(), text) if x) or "(A photo. Install a vision model in Models to read its text.)"
        doc = IncomingDoc(_title("Photo", note or text), body, image, "text/markdown", None, {"captured": "photo", "read_by": model or ""})
        result = await _run(kb_id, source_id, run_id, raw_dir, [doc], ctx)
        return {**result, "kb_id": kb_id, "read_by": model, "text": text[:500]}

    return job


# ---------- links and text ----------
async def text_or_link_job(text: str, title: str = ""):
    """A link is saved as the readable page; anything else as a note."""
    text = text.strip()
    if not text:
        raise CaptureError("Nothing to save")
    kb_id = await inbox_id()
    if URL_RE.match(text):
        from app import capture

        page = await capture.reader_view(text)
        return capture.capture_job(kb_id, text, title or page["title"], markdown=page["markdown"]), kb_id
    return text_job(kb_id, title or _title("Note", text), text), kb_id
